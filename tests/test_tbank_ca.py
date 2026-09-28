import hashlib
import os
import ssl
import unittest
from unittest import mock

from support import application_module


class TbankCaBundleTests(unittest.TestCase):
    ROOT_SHA256 = (
        "D26D2D0231B7C39F92CC738512BA5410"
        "3519E4405D68B5BD703E9788CA8ECF31"
    )

    def setUp(self):
        application_module._TBANK_CA_BUNDLE_PATH = None

    def test_bundled_root_is_the_official_russian_trusted_root_ca(self):
        with open(application_module.RUSSIAN_TRUSTED_ROOT_CA_PATH, encoding="ascii") as pem:
            der = ssl.PEM_cert_to_DER_cert(pem.read())
        self.assertEqual(hashlib.sha256(der).hexdigest().upper(), self.ROOT_SHA256)

    def test_bundle_keeps_public_roots_and_adds_the_russian_one(self):
        path = application_module._tbank_verify()
        self.assertTrue(os.path.isfile(path))
        combined = open(path, "rb").read()
        with open(application_module.RUSSIAN_TRUSTED_ROOT_CA_PATH, "rb") as pem:
            self.assertIn(pem.read().strip(), combined)
        import certifi
        with open(certifi.where(), "rb") as public:
            self.assertIn(public.read()[:400].strip(), combined)
        self.assertEqual(application_module._tbank_verify(), path)  # cached

    def test_env_override_wins(self):
        with mock.patch.dict(os.environ, {"TBANK_CA_BUNDLE": "/custom/ca.pem"}):
            self.assertEqual(application_module._tbank_verify(), "/custom/ca.pem")

    def test_tbank_requests_use_the_bundle(self):
        fake = mock.Mock()
        fake.return_value.status_code = 200
        fake.return_value.json.return_value = {"ok": True}
        with mock.patch.object(application_module.requests, "get", fake), \
                mock.patch.object(application_module.requests, "post", fake):
            application_module._tbank_request("/v1/statement", {}, token="t")
            application_module._tbank_request_post(
                "/v1/self-employed/recipients/list", {"limit": 1}, token="t"
            )
        verifies = [call.kwargs["verify"] for call in fake.call_args_list]
        self.assertEqual(len(verifies), 2)
        self.assertTrue(all(isinstance(v, str) and v.endswith(".pem") for v in verifies))


if __name__ == "__main__":
    unittest.main()
