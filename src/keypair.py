import pulumi
from Crypto.PublicKey import RSA


def create():
    """Create a new keypair."""
    key = RSA.generate(2048)
    private_key = key.exportKey("PEM")
    public_key = key.publickey().exportKey("OpenSSH")
    with open("key.pem", "w") as f:
        f.write(private_key.decode())
    with open("key.pub", "w") as f:
        f.write(public_key.decode())


def get_private_key(file_path: str) -> str:
    """Open and decdoe an existing private key."""
    path = Path(file_path)
    if path.exists() == False:
        path = path.expanduser()
    with open(path, mode="r") as f:
        private_key = f.read()
    return private_key

