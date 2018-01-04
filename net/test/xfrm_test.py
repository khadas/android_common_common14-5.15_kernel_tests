#!/usr/bin/python
#
# Copyright 2017 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint: disable=g-bad-todo,g-bad-file-header,wildcard-import
from errno import *  # pylint: disable=wildcard-import
from scapy import all as scapy
from socket import *  # pylint: disable=wildcard-import
import struct
import subprocess
import threading
import unittest

import csocket
import cstruct
import multinetwork_base
import net_test
import packets
import xfrm
import xfrm_base

ENCRYPTED_PAYLOAD = ("b1c74998efd6326faebe2061f00f2c750e90e76001664a80c287b150"
                     "59e74bf949769cc6af71e51b539e7de3a2a14cb05a231b969e035174"
                     "d98c5aa0cef1937db98889ec0d08fa408fecf616")

TEST_ADDR1 = "2001:4860:4860::8888"
TEST_ADDR2 = "2001:4860:4860::8844"

# IP addresses to use for tunnel endpoints. For generality, these should be
# different from the addresses we send packets to.
TUNNEL_ENDPOINTS = {4: "8.8.4.4", 6: TEST_ADDR2}

TEST_SPI = 0x1234



class XfrmFunctionalTest(xfrm_base.XfrmBaseTest):

  def assertIsUdpEncapEsp(self, packet, spi, seq, length):
    self.assertEquals(IPPROTO_UDP, packet.proto)
    udp_hdr = packet[scapy.UDP]
    self.assertEquals(4500, udp_hdr.dport)
    self.assertEquals(length, len(udp_hdr.load))
    esp_hdr, _ = cstruct.Read(str(udp_hdr.load), xfrm.EspHdr)
    # FIXME: this file currently swaps SPI byte order manually, so SPI needs to
    # be double-swapped here.
    self.assertEquals(xfrm.EspHdr((spi, seq)), esp_hdr)

  def testAddSa(self):
    self.xfrm.AddSaInfo("::", TEST_ADDR1, TEST_SPI, xfrm.XFRM_MODE_TRANSPORT,
                        3320, xfrm_base._ALGO_CBC_AES_256,
                        xfrm_base._ALGO_HMAC_SHA1, None, None, None, None)
    expected = (
        "src :: dst 2001:4860:4860::8888\n"
        "\tproto esp spi 0x00001234 reqid 3320 mode transport\n"
        "\treplay-window 4 \n"
        "\tauth-trunc hmac(sha1) 0x%s 96\n"
        "\tenc cbc(aes) 0x%s\n"
        "\tsel src ::/0 dst ::/0 \n" % (
            xfrm_base._AUTHENTICATION_KEY_128.encode("hex"),
            xfrm_base._ENCRYPTION_KEY_256.encode("hex")))

    actual = subprocess.check_output("ip xfrm state".split())
    try:
      self.assertMultiLineEqual(expected, actual)
    finally:
      self.xfrm.DeleteSaInfo(TEST_ADDR1, TEST_SPI, IPPROTO_ESP)

  def testFlush(self):
    self.assertEquals(0, len(self.xfrm.DumpSaInfo()))
    self.xfrm.AddSaInfo("::", "2000::", TEST_SPI, xfrm.XFRM_MODE_TRANSPORT,
                        1234, xfrm_base._ALGO_CBC_AES_256,
                        xfrm_base._ALGO_HMAC_SHA1, None, None, None, None)
    self.xfrm.AddSaInfo("0.0.0.0", "192.0.2.1", TEST_SPI,
                        xfrm.XFRM_MODE_TRANSPORT, 4321,
                        xfrm_base._ALGO_CBC_AES_256, xfrm_base._ALGO_HMAC_SHA1,
                        None, None, None, None)
    self.assertEquals(2, len(self.xfrm.DumpSaInfo()))
    self.xfrm.FlushSaInfo()
    self.assertEquals(0, len(self.xfrm.DumpSaInfo()))

  def _TestSocketPolicy(self, version):
    # Open a UDP socket and connect it.
    family = net_test.GetAddressFamily(version)
    s = socket(family, SOCK_DGRAM, 0)
    netid = self.RandomNetid()
    self.SelectInterface(s, netid, "mark")

    remoteaddr = self.GetRemoteAddress(version)
    s.connect((remoteaddr, 53))
    saddr, sport = s.getsockname()[:2]
    daddr, dport = s.getpeername()[:2]
    reqid = 0

    desc, pkt = packets.UDP(version, saddr, daddr, sport=sport)
    s.sendto(net_test.UDP_PAYLOAD, (remoteaddr, 53))
    self.ExpectPacketOn(netid, "Send after socket, expected %s" % desc, pkt)

    # Using IPv4 XFRM on a dual-stack socket requires setting an AF_INET policy
    # that's written in terms of IPv4 addresses.
    xfrm_version = 4 if version == 5 else version
    xfrm_family = net_test.GetAddressFamily(xfrm_version)
    xfrm_base.ApplySocketPolicy(s, xfrm_family, xfrm.XFRM_POLICY_OUT,
                                TEST_SPI, reqid, None)

    # Because the policy has level set to "require" (the default), attempting
    # to send a packet results in an error, because there is no SA that
    # matches the socket policy we set.
    self.assertRaisesErrno(
        EAGAIN,
        s.sendto, net_test.UDP_PAYLOAD, (remoteaddr, 53))

    # Adding a matching SA causes the packet to go out encrypted. The SA's
    # SPI must match the one in our template, and the destination address must
    # match the packet's destination address (in tunnel mode, it has to match
    # the tunnel destination).
    self.xfrm.AddSaInfo(net_test.GetWildcardAddress(xfrm_version),
                        self.GetRemoteAddress(xfrm_version),
                        TEST_SPI, xfrm.XFRM_MODE_TRANSPORT, reqid,
                        xfrm_base._ALGO_CBC_AES_256, xfrm_base._ALGO_HMAC_SHA1,
                        None, None, None, None)
    s.sendto(net_test.UDP_PAYLOAD, (remoteaddr, 53))
    expected_length = xfrm_base.GetEspPacketLength(xfrm.XFRM_MODE_TRANSPORT, 6,
                                                   None, net_test.UDP_PAYLOAD)
    expected_length = xfrm_base.GetEspPacketLength(xfrm.XFRM_MODE_TRANSPORT,
                                                   version, None,
                                                   net_test.UDP_PAYLOAD)
    self._ExpectEspPacketOn(netid, TEST_SPI, 1, expected_length, None, None)

    # Sending to another destination doesn't work: again, no matching SA.
    remoteaddr2 = self.GetOtherRemoteAddress(version)
    self.assertRaisesErrno(
        EAGAIN,
        s.sendto, net_test.UDP_PAYLOAD, (remoteaddr2, 53))

    # Sending on another socket without the policy applied results in an
    # unencrypted packet going out.
    s2 = socket(family, SOCK_DGRAM, 0)
    self.SelectInterface(s2, netid, "mark")
    s2.sendto(net_test.UDP_PAYLOAD, (remoteaddr, 53))
    pkts = self.ReadAllPacketsOn(netid)
    self.assertEquals(1, len(pkts))
    packet = pkts[0]

    protocol = packet.nh if version == 6 else packet.proto
    self.assertEquals(IPPROTO_UDP, protocol)

    # Deleting the SA causes the first socket to return errors again.
    self.xfrm.DeleteSaInfo(self.GetRemoteAddress(xfrm_version), TEST_SPI,
                           IPPROTO_ESP)
    self.assertRaisesErrno(
        EAGAIN,
        s.sendto, net_test.UDP_PAYLOAD, (remoteaddr, 53))

    # Clear the socket policy and expect a cleartext packet.
    xfrm_base.SetPolicySockopt(s, family, None)
    s.sendto(net_test.UDP_PAYLOAD, (remoteaddr, 53))
    self.ExpectPacketOn(netid, "Send after clear, expected %s" % desc, pkt)

    # Clearing the policy twice is safe.
    xfrm_base.SetPolicySockopt(s, family, None)
    s.sendto(net_test.UDP_PAYLOAD, (remoteaddr, 53))
    self.ExpectPacketOn(netid, "Send after clear 2, expected %s" % desc, pkt)

    # Clearing if a policy was never set is safe.
    s = socket(AF_INET6, SOCK_DGRAM, 0)
    xfrm_base.SetPolicySockopt(s, family, None)

  def testSocketPolicyIPv4(self):
    self._TestSocketPolicy(4)

  def testSocketPolicyIPv6(self):
    self._TestSocketPolicy(6)

  def testSocketPolicyMapped(self):
    self._TestSocketPolicy(5)

  # TODO: Should we completely re-write this using null encryption and null
  # authentication? We could then assemble and disassemble packets for each
  # direction individually. This approach would improve debuggability, avoid the
  # complexity of the twister, and allow the test to more-closely validate
  # deployable configurations.
  def testUdpEncapWithSocketPolicy(self):
    # TODO: test IPv6 instead of IPv4.
    netid = self.RandomNetid()
    myaddr = self.MyAddress(4, netid)
    remoteaddr = self.GetRemoteAddress(4)

    # Reserve a port on which to receive UDP encapsulated packets. Sending
    # packets works without this (and potentially can send packets with a source
    # port belonging to another application), but receiving requires the port to
    # be bound and the encapsulation socket option enabled.
    encap_socket = net_test.Socket(AF_INET, SOCK_DGRAM, 0)
    encap_socket.bind((myaddr, 0))
    encap_port = encap_socket.getsockname()[1]
    encap_socket.setsockopt(IPPROTO_UDP, xfrm.UDP_ENCAP,
                            xfrm.UDP_ENCAP_ESPINUDP)

    # Open a socket to send traffic.
    s = socket(AF_INET, SOCK_DGRAM, 0)
    self.SelectInterface(s, netid, "mark")
    s.connect((remoteaddr, 53))

    # Use the same SPI both inbound and outbound because this lets us receive
    # encrypted packets by simply replaying the packets the kernel sends.
    in_reqid = 123
    in_spi = TEST_SPI
    out_reqid = 456
    out_spi = TEST_SPI

    # Apply an outbound socket policy to s.
    xfrm_base.ApplySocketPolicy(s, AF_INET, xfrm.XFRM_POLICY_OUT,
                                out_spi, out_reqid, None)

    # Create inbound and outbound SAs that specify UDP encapsulation.
    encaptmpl = xfrm.XfrmEncapTmpl((xfrm.UDP_ENCAP_ESPINUDP, htons(encap_port),
                                    htons(4500), 16 * "\x00"))
    self.xfrm.AddSaInfo(myaddr, remoteaddr, out_spi,
                        xfrm.XFRM_MODE_TRANSPORT, out_reqid,
                        xfrm_base._ALGO_CBC_AES_256, xfrm_base._ALGO_HMAC_SHA1,
                        None, encaptmpl, None, None)

    # Add an encap template that's the mirror of the outbound one.
    encaptmpl.sport, encaptmpl.dport = encaptmpl.dport, encaptmpl.sport
    self.xfrm.AddSaInfo(remoteaddr, myaddr, in_spi, xfrm.XFRM_MODE_TRANSPORT,
                        in_reqid, xfrm_base._ALGO_CBC_AES_256,
                        xfrm_base._ALGO_HMAC_SHA1, None, encaptmpl, None, None)

    # Uncomment for debugging.
    # subprocess.call("ip xfrm state".split())

    # Now send a packet.
    s.sendto("foo", (remoteaddr, 53))
    srcport = s.getsockname()[1]
    # s.send("foo")  # TODO: WHY DOES THIS NOT WORK?

    # Expect to see an UDP encapsulated packet.
    pkts = self.ReadAllPacketsOn(netid)
    self.assertEquals(1, len(pkts))
    packet = pkts[0]
    self.assertIsUdpEncapEsp(packet, out_spi, 1, 52)

    # Now test the receive path. Because we don't know how to decrypt packets,
    # we just play back the encrypted packet that kernel sent earlier. We swap
    # the addresses in the IP header to make the packet look like it's bound for
    # us, but we can't do that for the port numbers because the UDP header is
    # part of the integrity protected payload, which we can only replay as is.
    # So the source and destination ports are swapped and the packet appears to
    # be sent from srcport to port 53. Open another socket on that port, and
    # apply the inbound policy to it.
    twisted_socket = socket(AF_INET, SOCK_DGRAM, 0)
    csocket.SetSocketTimeout(twisted_socket, 100)
    twisted_socket.bind(("0.0.0.0", 53))

    # TODO: why does this work without a per-socket policy applied?
    # The received  packet obviously matches an SA, but don't inbound packets
    # need to match a policy as well?

    # Save the payload of the packet so we can replay it back to ourselves, and
    # replace the SPI with our inbound SPI.
    payload = str(packet.payload)[8:]
    spi_seq = xfrm.EspHdr((in_spi, 1)).Pack()
    payload = spi_seq + payload[len(spi_seq):]

    # Tamper with the packet and check that it's dropped and counted as invalid.
    sainfo = self.xfrm.FindSaInfo(in_spi)
    self.assertEquals(0, sainfo.stats.integrity_failed)
    broken = payload[:25] + chr((ord(payload[25]) + 1) % 256) + payload[26:]
    incoming = (scapy.IP(src=remoteaddr, dst=myaddr) /
                scapy.UDP(sport=4500, dport=encap_port) / broken)
    self.ReceivePacketOn(netid, incoming)
    sainfo = self.xfrm.FindSaInfo(in_spi)
    self.assertEquals(1, sainfo.stats.integrity_failed)

    # Now play back the valid packet and check that we receive it.
    incoming = (scapy.IP(src=remoteaddr, dst=myaddr) /
                scapy.UDP(sport=4500, dport=encap_port) / payload)
    self.ReceivePacketOn(netid, incoming)
    data, src = twisted_socket.recvfrom(4096)
    self.assertEquals("foo", data)
    self.assertEquals((remoteaddr, srcport), src)

    # Check that unencrypted packets on twisted_socket are not received.
    unencrypted = (scapy.IP(src=remoteaddr, dst=myaddr) /
                   scapy.UDP(sport=srcport, dport=53) / "foo")
    self.assertRaisesErrno(EAGAIN, twisted_socket.recv, 4096)

  def testAllocSpecificSpi(self):
    spi = 0xABCD
    new_sa = self.xfrm.AllocSpi("::", IPPROTO_ESP, spi, spi)
    self.assertEquals(spi, new_sa.id.spi)

  def testAllocSpecificSpiUnavailable(self):
    """Attempt to allocate the same SPI twice."""
    spi = 0xABCD
    new_sa = self.xfrm.AllocSpi("::", IPPROTO_ESP, spi, spi)
    self.assertEquals(spi, new_sa.id.spi)
    with self.assertRaisesErrno(ENOENT):
      new_sa = self.xfrm.AllocSpi("::", IPPROTO_ESP, spi, spi)

  def testAllocRangeSpi(self):
    start, end = 0xABCD0, 0xABCDF
    new_sa = self.xfrm.AllocSpi("::", IPPROTO_ESP, start, end)
    spi = new_sa.id.spi
    self.assertGreaterEqual(spi, start)
    self.assertLessEqual(spi, end)

  def testAllocRangeSpiUnavailable(self):
    """Attempt to allocate N+1 SPIs from a range of size N."""
    start, end = 0xABCD0, 0xABCDF
    range_size = end - start + 1
    spis = set()
    # Assert that allocating SPI fails when none are available.
    with self.assertRaisesErrno(ENOENT):
      # Allocating range_size + 1 SPIs is guaranteed to fail.  Due to the way
      # kernel picks random SPIs, this has a high probability of failing before
      # reaching that limit.
      for i in xrange(range_size + 1):
        new_sa = self.xfrm.AllocSpi("::", IPPROTO_ESP, start, end)
        spi = new_sa.id.spi
        self.assertNotIn(spi, spis)
        spis.add(spi)

  def testSocketPolicyDstCacheV6(self):
    self._TestSocketPolicyDstCache(6)

  def testSocketPolicyDstCacheV4(self):
    self._TestSocketPolicyDstCache(4)

  def _TestSocketPolicyDstCache(self, version):
    """Test that destination cache is cleared with socket policy.

    This relies on the fact that connect() on a UDP socket populates the
    destination cache.
    """

    # Create UDP socket.
    family = net_test.GetAddressFamily(version)
    netid = self.RandomNetid()
    s = socket(family, SOCK_DGRAM, 0)
    self.SelectInterface(s, netid, "mark")

    # Populate the socket's destination cache.
    remote = self.GetRemoteAddress(version)
    s.connect((remote, 53))

    # Apply a policy to the socket. Should clear dst cache.
    reqid = 123
    xfrm_base.ApplySocketPolicy(s, family, xfrm.XFRM_POLICY_OUT,
                                TEST_SPI, reqid, None)

    # Policy with no matching SA should result in EAGAIN. If destination cache
    # failed to clear, then the UDP packet will be sent normally.
    with self.assertRaisesErrno(EAGAIN):
      s.send(net_test.UDP_PAYLOAD)
    self.ExpectNoPacketsOn(netid, "Packet not blocked by policy")

  def _CheckNullEncryptionTunnelMode(self, version):
    family = net_test.GetAddressFamily(version)
    netid = self.RandomNetid()
    local_addr = self.MyAddress(version, netid)
    remote_addr = self.GetRemoteAddress(version)

    # Borrow the address of another netId as the source address of the tunnel
    tun_local = self.MyAddress(version, self.RandomNetid(netid))
    # For generality, pick a tunnel endpoint that's not the address we
    # connect the socket to.
    tun_remote = TUNNEL_ENDPOINTS[version]

    # Output
    self.xfrm.AddSaInfo(
        tun_local, tun_remote, 0xABCD, xfrm.XFRM_MODE_TUNNEL, 123,
        xfrm_base._ALGO_CRYPT_NULL, xfrm_base._ALGO_AUTH_NULL,
        None, None, None, netid)
    # Input
    self.xfrm.AddSaInfo(
        tun_remote, tun_local, 0x9876, xfrm.XFRM_MODE_TUNNEL, 456,
        xfrm_base._ALGO_CRYPT_NULL, xfrm_base._ALGO_AUTH_NULL,
        None, None, None, None)

    sock = net_test.UDPSocket(family)
    self.SelectInterface(sock, netid, "mark")
    sock.bind((local_addr, 0))
    local_port = sock.getsockname()[1]
    remote_port = 5555

    xfrm_base.ApplySocketPolicy(
        sock, family, xfrm.XFRM_POLICY_OUT, 0xABCD, 123,
        (tun_local, tun_remote))
    xfrm_base.ApplySocketPolicy(
        sock, family, xfrm.XFRM_POLICY_IN, 0x9876, 456,
        (tun_remote, tun_local))

    # Create and receive an ESP packet.
    IpType = {4: scapy.IP, 6: scapy.IPv6}[version]
    input_pkt = (IpType(src=remote_addr, dst=local_addr) /
                 scapy.UDP(sport=remote_port, dport=local_port) /
                 "input hello")
    input_pkt = IpType(str(input_pkt)) # Compute length, checksum.
    input_pkt = xfrm_base.EncryptPacketWithNull(input_pkt, 0x9876,
                                                1, (tun_remote, tun_local))

    self.ReceivePacketOn(netid, input_pkt)
    msg, addr = sock.recvfrom(1024)
    self.assertEquals("input hello", msg)
    self.assertEquals((remote_addr, remote_port), addr[:2])

    # Send and capture a packet.
    sock.sendto("output hello", (remote_addr, remote_port))
    packets = self.ReadAllPacketsOn(netid)
    self.assertEquals(1, len(packets))
    output_pkt = packets[0]
    output_pkt, esp_hdr = xfrm_base.DecryptPacketWithNull(output_pkt)
    self.assertEquals(output_pkt[scapy.UDP].len, len("output_hello") + 8)
    self.assertEquals(remote_addr, output_pkt.dst)
    self.assertEquals(remote_port, output_pkt[scapy.UDP].dport)
    # length of the payload plus the UDP header
    self.assertEquals("output hello", str(output_pkt[scapy.UDP].payload))
    self.assertEquals(0xABCD, esp_hdr.spi)

  def testNullEncryptionTunnelMode(self):
    """Verify null encryption in tunnel mode.

    This test verifies both manual assembly and disassembly of UDP packets
    with ESP in IPsec tunnel mode.
    """
    for version in [4, 6]:
      self._CheckNullEncryptionTunnelMode(version)

  def _CheckNullEncryptionTransportMode(self, version):
    family = net_test.GetAddressFamily(version)
    netid = self.RandomNetid()
    local_addr = self.MyAddress(version, netid)
    remote_addr = self.GetRemoteAddress(version)

    # Output
    self.xfrm.AddSaInfo(
        local_addr, remote_addr, 0xABCD, xfrm.XFRM_MODE_TRANSPORT, 123,
        xfrm_base._ALGO_CRYPT_NULL, xfrm_base._ALGO_AUTH_NULL,
        None, None, None, None)
    # Input
    self.xfrm.AddSaInfo(
        remote_addr, local_addr, 0x9876, xfrm.XFRM_MODE_TRANSPORT, 456,
        xfrm_base._ALGO_CRYPT_NULL, xfrm_base._ALGO_AUTH_NULL,
        None, None, None, None)

    sock = net_test.UDPSocket(family)
    self.SelectInterface(sock, netid, "mark")
    sock.bind((local_addr, 0))
    local_port = sock.getsockname()[1]
    remote_port = 5555

    xfrm_base.ApplySocketPolicy(
        sock, family, xfrm.XFRM_POLICY_OUT, 0xABCD, 123, None)
    xfrm_base.ApplySocketPolicy(
        sock, family, xfrm.XFRM_POLICY_IN, 0x9876, 456, None)

    # Create and receive an ESP packet.
    IpType = {4: scapy.IP, 6: scapy.IPv6}[version]
    input_pkt = (IpType(src=remote_addr, dst=local_addr) /
                 scapy.UDP(sport=remote_port, dport=local_port) /
                 "input hello")
    input_pkt = IpType(str(input_pkt)) # Compute length, checksum.
    input_pkt = xfrm_base.EncryptPacketWithNull(input_pkt, 0x9876, 1, None)

    self.ReceivePacketOn(netid, input_pkt)
    msg, addr = sock.recvfrom(1024)
    self.assertEquals("input hello", msg)
    self.assertEquals((remote_addr, remote_port), addr[:2])

    # Send and capture a packet.
    sock.sendto("output hello", (remote_addr, remote_port))
    packets = self.ReadAllPacketsOn(netid)
    self.assertEquals(1, len(packets))
    output_pkt = packets[0]
    output_pkt, esp_hdr = xfrm_base.DecryptPacketWithNull(output_pkt)
    # length of the payload plus the UDP header
    self.assertEquals(output_pkt[scapy.UDP].len, len("output_hello") + 8)
    self.assertEquals(remote_addr, output_pkt.dst)
    self.assertEquals(remote_port, output_pkt[scapy.UDP].dport)
    self.assertEquals("output hello", str(output_pkt[scapy.UDP].payload))
    self.assertEquals(0xABCD, esp_hdr.spi)

  def testNullEncryptionTransportMode(self):
    """Verify null encryption in transport mode.

    This test verifies both manual assembly and disassembly of UDP packets
    with ESP in IPsec transport mode.
    """
    for version in [4, 6]:
      self._CheckNullEncryptionTransportMode(version)

  def _CheckGlobalPoliciesByMark(self, version):
    """Tests that global policies may differ by only the mark."""
    family = net_test.GetAddressFamily(version)
    sel = xfrm.EmptySelector(family)
    # Pick 2 arbitrary mark values.
    mark1 = xfrm.XfrmMark(mark=0xf00, mask=xfrm_base.MARK_MASK_ALL)
    mark2 = xfrm.XfrmMark(mark=0xf00d, mask=xfrm_base.MARK_MASK_ALL)
    # Create a global policy.
    policy = xfrm_base.UserPolicy(xfrm.XFRM_POLICY_OUT, sel)
    tmpl = xfrm_base.UserTemplate(AF_UNSPEC, 0xfeed, 0, None)
    # Create the policy with the first mark.
    self.xfrm.AddPolicyInfo(policy, tmpl, mark1)
    # Create the same policy but with the second (different) mark.
    self.xfrm.AddPolicyInfo(policy, tmpl, mark2)
    # Delete the policies individually
    self.xfrm.DeletePolicyInfo(sel, xfrm.XFRM_POLICY_OUT, mark1)
    self.xfrm.DeletePolicyInfo(sel, xfrm.XFRM_POLICY_OUT, mark2)

  def testGlobalPoliciesByMarkV4(self):
    self._CheckGlobalPoliciesByMark(4)

  def testGlobalPoliciesByMarkV6(self):
    self._CheckGlobalPoliciesByMark(6)

  def _CheckUpdatePolicy(self, version):
    """Tests that we can can update the template on a policy."""
    family = net_test.GetAddressFamily(version)
    tmpl1 = xfrm_base.UserTemplate(family, 0xdead, 0, None)
    tmpl2 = xfrm_base.UserTemplate(family, 0xbeef, 0, None)
    sel = xfrm.EmptySelector(family)
    policy = xfrm_base.UserPolicy(xfrm.XFRM_POLICY_OUT, sel)
    mark = xfrm.XfrmMark(mark=0xf00, mask=xfrm_base.MARK_MASK_ALL)

    def _CheckTemplateMatch(tmpl):
      """Dump the SPD and match a single template on a single policy."""
      dump = self.xfrm.DumpPolicyInfo()
      self.assertEquals(1, len(dump))
      _, attributes = dump[0]
      self.assertEquals(attributes['XFRMA_TMPL'], tmpl)

    # Create a new policy using update.
    self.xfrm.UpdatePolicyInfo(policy, tmpl1, mark)
    # NEWPOLICY will not update the existing policy. This checks both that
    # UPDPOLICY created a policy and that NEWPOLICY will not perform updates.
    _CheckTemplateMatch(tmpl1)
    with self.assertRaisesErrno(EEXIST):
      self.xfrm.AddPolicyInfo(policy, tmpl2, mark)
    # Update the policy using UPDPOLICY.
    self.xfrm.UpdatePolicyInfo(policy, tmpl2, mark)
    # There should only be one policy after update, and it should have the
    # updated template.
    _CheckTemplateMatch(tmpl2)

  def testUpdatePolicyV4(self):
    self._CheckUpdatePolicy(4)

  def testUpdatePolicyV6(self):
    self._CheckUpdatePolicy(6)

  def _CheckPolicyDifferByDirection(self,version):
    """Tests that policies can differ only by direction."""
    family = net_test.GetAddressFamily(version)
    tmpl = xfrm_base.UserTemplate(family, 0xdead, 0, None)
    sel = xfrm.EmptySelector(family)
    mark = xfrm.XfrmMark(mark=0xf00, mask=xfrm_base.MARK_MASK_ALL)
    policy = xfrm_base.UserPolicy(xfrm.XFRM_POLICY_OUT, sel)
    self.xfrm.AddPolicyInfo(policy, tmpl, mark)
    policy = xfrm_base.UserPolicy(xfrm.XFRM_POLICY_IN, sel)
    self.xfrm.AddPolicyInfo(policy, tmpl, mark)

  def testPolicyDifferByDirectionV4(self):
    self._CheckPolicyDifferByDirection(4)

  def testPolicyDifferByDirectionV6(self):
    self._CheckPolicyDifferByDirection(6)

class XfrmOutputMarkTest(xfrm_base.XfrmBaseTest):

  def _CheckTunnelModeOutputMark(self, version, tunsrc, mark, expected_netid):
    """Tests sending UDP packets to tunnel mode SAs with output marks.

    Opens a UDP socket and binds it to a random netid, then sets up tunnel mode
    SAs with an output_mark of mark and sets a socket policy to use the SA.
    Then checks that sending on those SAs sends a packet on expected_netid,
    or, if expected_netid is zero, checks that sending returns ENETUNREACH.

    Args:
      version: 4 or 6.
      tunsrc: A string, the source address of the tunnel.
      mark: An integer, the output_mark to set in the SA.
      expected_netid: An integer, the netid to expect the kernel to send the
          packet on. If None, expect that sendto will fail with ENETUNREACH.
    """
    # Open a UDP socket and bind it to a random netid.
    family = net_test.GetAddressFamily(version)
    s = socket(family, SOCK_DGRAM, 0)
    self.SelectInterface(s, self.RandomNetid(), "mark")

    # For generality, pick a tunnel endpoint that's not the address we
    # connect the socket to.
    tundst = TUNNEL_ENDPOINTS[version]
    tun_addrs = (tunsrc, tundst)

    # Create a tunnel mode SA and use XFRM_OUTPUT_MARK to bind it to netid.
    spi = TEST_SPI * mark
    reqid = 100 + spi
    self.xfrm.AddSaInfo(tunsrc, tundst, spi, xfrm.XFRM_MODE_TUNNEL, reqid,
                        xfrm_base._ALGO_CBC_AES_256, xfrm_base._ALGO_HMAC_SHA1,
                        None, None, None, mark)

    # Set a socket policy to use it.
    xfrm_base.ApplySocketPolicy(s, family, xfrm.XFRM_POLICY_OUT, spi, reqid,
                                tun_addrs)

    # Send a packet and check that we see it on the wire.
    remoteaddr = self.GetRemoteAddress(version)

    packetlen = xfrm_base.GetEspPacketLength(xfrm.XFRM_MODE_TUNNEL, version,
                                             IPPROTO_IP, net_test.UDP_PAYLOAD)

    if expected_netid is not None:
      s.sendto(net_test.UDP_PAYLOAD, (remoteaddr, 53))
      self._ExpectEspPacketOn(expected_netid, spi, 1, packetlen, tunsrc, tundst)
    else:
      with self.assertRaisesErrno(ENETUNREACH):
        s.sendto(net_test.UDP_PAYLOAD, (remoteaddr, 53))

  def testTunnelModeOutputMarkIPv4(self):
    for netid in self.NETIDS:
      tunsrc = self.MyAddress(4, netid)
      self._CheckTunnelModeOutputMark(4, tunsrc, netid, netid)

  def testTunnelModeOutputMarkIPv6(self):
    for netid in self.NETIDS:
      tunsrc = self.MyAddress(6, netid)
      self._CheckTunnelModeOutputMark(6, tunsrc, netid, netid)

  def testTunnelModeOutputNoMarkIPv4(self):
    tunsrc = self.MyAddress(4, self.RandomNetid())
    self._CheckTunnelModeOutputMark(4, tunsrc, 0, None)

  def testTunnelModeOutputNoMarkIPv6(self):
    tunsrc = self.MyAddress(6, self.RandomNetid())
    self._CheckTunnelModeOutputMark(6, tunsrc, 0, None)

  def testTunnelModeOutputInvalidMarkIPv4(self):
    tunsrc = self.MyAddress(4, self.RandomNetid())
    self._CheckTunnelModeOutputMark(4, tunsrc, 9999, None)

  def testTunnelModeOutputInvalidMarkIPv6(self):
    tunsrc = self.MyAddress(6, self.RandomNetid())
    self._CheckTunnelModeOutputMark(6, tunsrc, 9999, None)

  def testTunnelModeOutputMarkAttributes(self):
    mark = 1234567
    self.xfrm.AddSaInfo(TEST_ADDR1, TUNNEL_ENDPOINTS[6], 0x1234,
                        xfrm.XFRM_MODE_TUNNEL, 100, xfrm_base._ALGO_CBC_AES_256,
                        xfrm_base._ALGO_HMAC_SHA1, None, None, None, mark)
    dump = self.xfrm.DumpSaInfo()
    self.assertEquals(1, len(dump))
    sainfo, attributes = dump[0]
    self.assertEquals(mark, attributes["XFRMA_OUTPUT_MARK"])

  def testInvalidAlgorithms(self):
    key = "af442892cdcd0ef650e9c299f9a8436a".decode("hex")
    invalid_auth = (xfrm.XfrmAlgoAuth(("invalid(algo)", 128, 96)), key)
    invalid_crypt = (xfrm.XfrmAlgo(("invalid(algo)", 128)), key)
    with self.assertRaisesErrno(ENOSYS):
        self.xfrm.AddSaInfo(TEST_ADDR1, TEST_ADDR2, 0x1234,
            xfrm.XFRM_MODE_TRANSPORT, 0, xfrm_base._ALGO_CBC_AES_256,
            invalid_auth, None, None, None, 0)
    with self.assertRaisesErrno(ENOSYS):
        self.xfrm.AddSaInfo(TEST_ADDR1, TEST_ADDR2, 0x1234,
            xfrm.XFRM_MODE_TRANSPORT, 0, invalid_crypt,
            xfrm_base._ALGO_HMAC_SHA1, None, None, None, 0)


if __name__ == "__main__":
  unittest.main()
