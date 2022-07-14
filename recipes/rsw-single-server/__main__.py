import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pulumi
from Crypto.PublicKey import RSA
from pulumi_aws import ec2
from pulumi_command import remote
from rich import inspect, print

# ------------------------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------------------------

@dataclass 
class ConfigValues:
    """A single object to manage all config files."""
    config: pulumi.Config = field(default_factory=lambda: pulumi.Config())
    email: str = field(init=False)
    rsw_license: str = field(init=False)
    public_key: str = field(init=False)

    def __post_init__(self):
        self.email = self.config.require("email")
        self.rsw_license = self.config.require("rsw_license")
        self.public_key = self.config.require("public_key")   


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
        key_name=f"{config.email}-keypair-for-pulumi",
        public_key=config.public_key,
        tags=tags | {"Name": f"{config.email}-key-pair"},
    )

    rsw_server = ec2.Instance(
        f"rstudio workbench server",
        instance_type="t3.medium",
        vpc_security_group_ids=[security_group.id],
        # Ubuntu Server 20.04 LTS (HVM), SSD Volume Type for us-east-2
        ami="ami-0fb653ca2d3203ac1",                 
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
    # Install required software one each server
    # --------------------------------------------------------------------------
    
    command_set_environment_variables = remote.Command(
        "set environment variables", 
        create=pulumi.Output.concat(
            'echo "export RSW_LICENSE=', config.rsw_license, '" > .env;',
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
    # Build
    # --------------------------------------------------------------------------

    command_build_rsw = remote.Command(
        f"build rsw", 
        create="""export PATH="$PATH:$HOME/bin"; just build-rsw""", 
        connection=connection, 
        opts=pulumi.ResourceOptions(depends_on=[command_copy_justfile])
    )

main()
