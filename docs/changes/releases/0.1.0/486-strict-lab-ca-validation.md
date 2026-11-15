# Generate disposable lab CAs compatible with strict TLS validation

Include the critical certificate-signing Key Usage extension on generated lab
CAs, so clients using OpenSSL strict validation, including Python 3.14's default
SSL context, can authenticate the disposable servers without weakening TLS.
Existing lab identities are not rotated; prepare a fresh lab to use the corrected
certificate generator.
