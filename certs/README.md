# Trusted certificates

`russian_trusted_root_ca.pem` — the Russian Trusted Root CA (Минцифры России,
valid until 2032-02-27, SHA-256 D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31),
downloaded from the official https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt.

`business.tbank.ru` serves a certificate issued under this root, which is not
in the public CA bundle (certifi) — without it requests to the T-Bank API fail
with `CERTIFICATE_VERIFY_FAILED: self signed certificate in certificate chain`.
It is added on top of certifi only for T-Bank requests (see `_tbank_verify` in
app.py); nothing else in the app trusts it.
