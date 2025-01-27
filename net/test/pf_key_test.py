#!/usr/bin/python3
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
from socket import *
import unittest

import binascii
import csocket
import pf_key
import xfrm

ENCRYPTION_KEY = binascii.unhexlify("308146eb3bd84b044573d60f5a5fd159"
                                    "57c7d4fe567a2120f35bae0f9869ec22")
AUTH_KEY = binascii.unhexlify("af442892cdcd0ef650e9c299f9a8436a")


class PfKeyTest(unittest.TestCase):

  def setUp(self):
    self.pf_key = pf_key.PfKey()
    self.xfrm = xfrm.Xfrm()

  def tearDown(self):
    self.pf_key.close()
    self.pf_key = None

  def testAddDelSa(self):
    src4 = csocket.Sockaddr(("192.0.2.1", 0))
    dst4 = csocket.Sockaddr(("192.0.2.2", 1))
    self.pf_key.AddSa(src4, dst4, 0xdeadbeef, pf_key.SADB_TYPE_ESP,
                      pf_key.IPSEC_MODE_TRANSPORT, 54321,
                      pf_key.SADB_X_EALG_AESCBC, ENCRYPTION_KEY,
                      pf_key.SADB_X_AALG_SHA2_256HMAC, ENCRYPTION_KEY)

    src6 = csocket.Sockaddr(("2001:db8::1", 0))
    dst6 = csocket.Sockaddr(("2001:db8::2", 0))
    self.pf_key.AddSa(src6, dst6, 0xbeefdead, pf_key.SADB_TYPE_ESP,
                      pf_key.IPSEC_MODE_TRANSPORT, 12345,
                      pf_key.SADB_X_EALG_AESCBC, ENCRYPTION_KEY,
                      pf_key.SADB_X_AALG_SHA2_256HMAC, ENCRYPTION_KEY)

    sainfos = self.xfrm.DumpSaInfo()
    self.assertEqual(2, len(sainfos))
    state4, attrs4 = [(s, a) for s, a in sainfos if s.family == AF_INET][0]
    state6, attrs6 = [(s, a) for s, a in sainfos if s.family == AF_INET6][0]

    pfkey_sainfos = self.pf_key.DumpSaInfo()
    self.assertEqual(2, len(pfkey_sainfos))
    self.assertTrue(all(msg.satype == pf_key.SDB_TYPE_ESP)
                    for msg, _ in pfkey_sainfos)

    self.assertEqual(xfrm.IPPROTO_ESP, state4.id.proto)
    self.assertEqual(xfrm.IPPROTO_ESP, state6.id.proto)
    self.assertEqual(54321, state4.reqid)
    self.assertEqual(12345, state6.reqid)
    self.assertEqual(0xdeadbeef, state4.id.spi)
    self.assertEqual(0xbeefdead, state6.id.spi)

    self.assertEqual(xfrm.PaddedAddress("192.0.2.1"), state4.saddr)
    self.assertEqual(xfrm.PaddedAddress("192.0.2.2"), state4.id.daddr)
    self.assertEqual(xfrm.PaddedAddress("2001:db8::1"), state6.saddr)
    self.assertEqual(xfrm.PaddedAddress("2001:db8::2"), state6.id.daddr)

    # The algorithm names are null-terminated, but after that contain garbage.
    # Kernel bug?
    aes_name = b"cbc(aes)\x00"
    sha256_name = b"hmac(sha256)\x00"
    self.assertTrue(attrs4["XFRMA_ALG_CRYPT"].name.startswith(aes_name))
    self.assertTrue(attrs6["XFRMA_ALG_CRYPT"].name.startswith(aes_name))
    self.assertTrue(attrs4["XFRMA_ALG_AUTH"].name.startswith(sha256_name))
    self.assertTrue(attrs6["XFRMA_ALG_AUTH"].name.startswith(sha256_name))

    self.assertEqual(256, attrs4["XFRMA_ALG_CRYPT"].key_len)
    self.assertEqual(256, attrs6["XFRMA_ALG_CRYPT"].key_len)
    self.assertEqual(256, attrs4["XFRMA_ALG_AUTH"].key_len)
    self.assertEqual(256, attrs6["XFRMA_ALG_AUTH"].key_len)
    self.assertEqual(256, attrs4["XFRMA_ALG_AUTH_TRUNC"].key_len)
    self.assertEqual(256, attrs6["XFRMA_ALG_AUTH_TRUNC"].key_len)

    if attrs4["XFRMA_ALG_AUTH_TRUNC"].trunc_len == 96:
        missing4 = True
    else:
        self.assertEqual(128, attrs4["XFRMA_ALG_AUTH_TRUNC"].trunc_len)
        missing4 = False

    if attrs6["XFRMA_ALG_AUTH_TRUNC"].trunc_len == 96:
        missing6 = True
    else:
        self.assertEqual(128, attrs6["XFRMA_ALG_AUTH_TRUNC"].trunc_len)
        missing6 = False

    self.pf_key.DelSa(src4, dst4, 0xdeadbeef, pf_key.SADB_TYPE_ESP)
    self.assertEqual(1, len(self.xfrm.DumpSaInfo()))
    self.pf_key.DelSa(src6, dst6, 0xbeefdead, pf_key.SADB_TYPE_ESP)
    self.assertEqual(0, len(self.xfrm.DumpSaInfo()))

    if missing4 or missing6:
        self.assertFalse("missing b8a72fd7c4e9 ANDROID: net: xfrm: make PF_KEY SHA256 use RFC-compliant truncation.")


if __name__ == "__main__":
  unittest.main()
