from dataclasses import dataclass, field
from pathlib import Path

import pulumi
import pulumi_tls as tls
import requests
import jinja2
from pulumi_aws import ec2
from pulumi_command import remote
from rich import print, inspect
import hashlib
from Crypto.PublicKey import RSA


# ------------------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------------------

@dataclass 
class ConfigValues:
    """A single object to manage all config files."""
    config: pulumi.Config = field(default_factory=lambda: pulumi.Config())
    email: str = field(init=False)
    rsw_license: str = field(init=False)
    daily: bool = field(init=False)
    ssl: bool = field(init=False)
    public_key: str = field(init=False)

    def __post_init__(self):
        self.email = self.config.require("email")
        self.rsw_license = self.config.require("rsw_license")
        self.daily = self.config.require("daily").lower() in ("yes", "true", "t", "1")
        self.ssl = self.config.require("ssl").lower() in ("yes", "true", "t", "1")
        self.public_key = self.config.require("public_key")   


def get_private_key(file_path: str) -> str:
    path = Path(file_path)
    if path.exists() == False:
        path = path.expanduser()
    with open(path, mode="r") as f:
        private_key = f.read()
    return private_key


def get_latest_build(daily: bool) -> str:
    if daily:
        url = "https://dailies.rstudio.com/rstudio/latest/index.json"
        r = requests.get(url)
        data = r.json()
        link = data["products"]["workbench"]["platforms"]["bionic"]["link"]
        filename = data["products"]["workbench"]["platforms"]["bionic"]["filename"]
    else:
        link = "https://download2.rstudio.org/server/bionic/amd64/rstudio-workbench-2022.02.3-492.pro3-amd64.deb"
        filename = "rstudio-workbench-2022.02.3-492.pro3-amd64.deb"
    return (link, filename)


def create_template(path: str) -> jinja2.Template:
    with open(path, 'r') as f:
        template = jinja2.Template(f.read())
    return template


def hash_file(path: str) -> pulumi.Output:
    with open(path, mode="r") as f:
        text = f.read()
    hash_str = hashlib.sha224(bytes(text, encoding='utf-8')).hexdigest()
    return pulumi.Output.concat(hash_str)


# ------------------------------------------------------------------------------
# Infrastructure
# ------------------------------------------------------------------------------

def main():
    # --------------------------------------------------------------------------
    # Get configuration values
    # --------------------------------------------------------------------------
    config = ConfigValues()

    tags = {
        "rs:environment": "development",
        "rs:owner": config.email,
        "rs:project": "solutions",
    }

    # --------------------------------------------------------------------------
    # Make security groups
    # --------------------------------------------------------------------------
    security_group = ec2.SecurityGroup(
        "security group",
        description= config.email + " security group for Pulumi deployment",
        ingress=[
            {"protocol": "TCP", "from_port": 22, "to_port": 22, 'cidr_blocks': ['0.0.0.0/0'], "description": "SSH"},
            {"protocol": "TCP", "from_port": 8787, "to_port": 8787, 'cidr_blocks': ['0.0.0.0/0'], "description": "RSW"},
            {"protocol": "TCP", "from_port": 443, "to_port": 443, 'cidr_blocks': ['0.0.0.0/0'], "description": "HTTPS"},
            {"protocol": "TCP", "from_port": 80, "to_port": 80, 'cidr_blocks': ['0.0.0.0/0'], "description": "HTTP"},
        ],
        egress=[
            {"protocol": "All", "from_port": -1, "to_port": -1, 'cidr_blocks': ['0.0.0.0/0'], "description": "Allow all outbound traffic"},
        ],
        tags=tags | {"Name": f"{config.email}-rsw-single-server"},
    )

    # --------------------------------------------------------------------------
    # Stand up the servers
    # --------------------------------------------------------------------------
    key_pair = ec2.KeyPair(
        "ec2 key pair",
        key_name="samedwardes-keypair-for-pulumi",
        public_key=config.public_key,
        tags=tags | {"Name": f"{config.email}-key-pair"},
    )

    # Ubuntu Server 20.04 LTS (HVM), SSD Volume Type for us-east-2
    ami_id = "ami-0fb653ca2d3203ac1"

    rsw_server = ec2.Instance(
        f"rstudio workbench server",
        instance_type="t3.medium",
        vpc_security_group_ids=[security_group.id],
        ami=ami_id,                 
        tags=tags | {"Name": f"{config.email}-rsw-server"},
        key_name=key_pair.key_name
    )

    connection = remote.ConnectionArgs(
        host=rsw_server.public_dns, 
        user="ubuntu", 
        private_key=Path("key.pem").read_text()
    )

    # Export final pulumi variables.
    pulumi.export('rsw_public_ip', rsw_server.public_ip)
    pulumi.export('rsw_public_dns', rsw_server.public_dns)
    pulumi.export('rsw_subnet_id', rsw_server.subnet_id)
  
    # --------------------------------------------------------------------------
    # Create a self signed cert
    # --------------------------------------------------------------------------
    # Create a new private CA
    ca_private_key = tls.PrivateKey(
        "private key for ssl",
        algorithm="RSA",
        rsa_bits="2048"
    )

    # Create a self signed cert
    ca_cert = tls.SelfSignedCert(
        "self signed cert for ssl",
        private_key_pem=ca_private_key.private_key_pem,
        is_ca_certificate=False,
        validity_period_hours=8760,
        allowed_uses=[
            "key_encipherment",
            "digital_signature",
            "cert_signing"
        ],
        dns_names=[rsw_server.public_dns],
        subject=tls.SelfSignedCertSubjectArgs(
            common_name="private-ca",
            organization="RStudio"
        )
    )

    tls_crt_setup = remote.Command(
        "write ~/server.crt for ssl",
        create=pulumi.Output.concat('echo "', ca_cert.cert_pem, '" > ~/server.crt'),
        connection=connection, 
        opts=pulumi.ResourceOptions(depends_on=[rsw_server, ca_cert, ca_private_key])
    )

    tls_key_setup = remote.Command(
        "write ~/server.key for ssl",
        create=pulumi.Output.concat('echo "', ca_private_key.private_key_pem, '" > ~/server.key',),
        connection=connection, 
        opts=pulumi.ResourceOptions(depends_on=[rsw_server, ca_cert, ca_private_key])
    )

    # --------------------------------------------------------------------------
    # Install required software one each server
    # --------------------------------------------------------------------------
    
    rsw_url, rsw_filename = get_latest_build(config.daily)
    
    command_set_environment_variables = remote.Command(
        "set environment variables", 
        create=pulumi.Output.concat(
            'echo "export RSW_LICENSE=', config.rsw_license, '" > .env;',
            'echo "export RSW_URL=', rsw_url,'" >> .env;',
            'echo "export RSW_FILENAME=', rsw_filename, '" >> .env;',
        ), 
        connection=connection, 
        opts=pulumi.ResourceOptions(depends_on=[rsw_server])
    )

    command_install_justfile = remote.Command(
        f"install justfile",
        create="\n".join([
            """curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to ~/bin;""",
            """echo 'export PATH="$PATH:$HOME/bin"' >> ~/.bashrc;"""
        ]),
        connection=connection,
        opts=pulumi.ResourceOptions(depends_on=[rsw_server])
    )

    command_copy_justfile = remote.CopyFile(
        f"copy ~/justfile",  
        local_path="server-side-files/justfile", 
        remote_path='justfile', 
        connection=connection, 
        opts=pulumi.ResourceOptions(depends_on=[rsw_server]),
        triggers=[hash_file("server-side-files/justfile")]
    )

    # --------------------------------------------------------------------------
    # Config files
    # --------------------------------------------------------------------------
    file_path_rserver = "server-side-files/config/rserver.conf"
    copy_rserver_conf = remote.Command(
        "copy ~/rserver.conf",
        create=pulumi.Output.concat(
            'echo "', 
            pulumi.Output.all(rsw_server.public_ip).apply(lambda x: create_template(file_path_rserver).render(ssl=config.ssl)), 
            '" > ~/rserver.conf'
        ),
        connection=connection, 
        opts=pulumi.ResourceOptions(depends_on=[rsw_server]),
        triggers=[hash_file(file_path_rserver)]
    )
    
    file_path_launcher = "server-side-files/config/launcher.conf"
    copy_launcher_conf = remote.Command(
        "copy ~/launcher.conf",
        create=pulumi.Output.concat(
            'echo "', 
            pulumi.Output.all(rsw_server.public_ip).apply(lambda x: create_template(file_path_launcher).render()), 
            '" > ~/launcher.conf'
        ),
        connection=connection, 
        opts=pulumi.ResourceOptions(depends_on=[rsw_server]),
        triggers=[hash_file(file_path_launcher)]
    )
    
    file_path_vscode = "server-side-files/config/vscode.extensions.conf"
    copy_vscode_conf = remote.Command(
        "copy ~/vscode.extensions.conf",
        create=pulumi.Output.concat(
            'echo "', 
            pulumi.Output.all(rsw_server.public_ip).apply(lambda x: create_template(file_path_vscode).render()), 
            '" > ~/vscode.extensions.conf'
        ),
        connection=connection, 
        opts=pulumi.ResourceOptions(depends_on=[rsw_server]),
        triggers=[hash_file(file_path_vscode)]
    )

    # --------------------------------------------------------------------------
    # Build
    # --------------------------------------------------------------------------

    command_build_rsw = remote.Command(
        f"build rsw", 
        create="""export PATH="$PATH:$HOME/bin"; just build-rsw""", 
        connection=connection, 
        opts=pulumi.ResourceOptions(depends_on=[command_copy_justfile])
    )

main()