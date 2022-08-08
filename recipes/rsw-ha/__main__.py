"""An AWS Python Pulumi program"""

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import jinja2
import pulumi
from pulumi_aws import ec2, efs, rds
from pulumi_command import remote

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
# Infrastructure functions
# ------------------------------------------------------------------------------

def make_rsw_server(
    name: str, 
    tags: Dict, 
    key_pair: ec2.KeyPair, 
    vpc_group_ids: List[str]
):
    # Stand up a server.
    server = ec2.Instance(
        f"rstudio-workbench-{name}",
        instance_type="t3.medium",
        vpc_security_group_ids=vpc_group_ids,
        ami="ami-0fb653ca2d3203ac1",  # Ubuntu Server 20.04 LTS (HVM), SSD Volume Type
        tags=tags,
        key_name=key_pair.key_name
    )
    
    # Export final pulumi variables.
    pulumi.export(f'rsw_{name}_public_ip', server.public_ip)
    pulumi.export(f'rsw_{name}_public_dns', server.public_dns)
    pulumi.export(f'rsw_{name}_subnet_id', server.subnet_id)

    return server


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
    # Set up keys.
    # --------------------------------------------------------------------------
    key_pair = ec2.KeyPair(
        "ec2 key pair",
        key_name=f"{config.email}-keypair-for-pulumi",
        public_key=config.public_key,
        tags=tags | {"Name": f"{config.email}-key-pair"},
    )
    
    # --------------------------------------------------------------------------
    # Make security groups
    # --------------------------------------------------------------------------
    rsw_security_group = ec2.SecurityGroup(
        "rsw-ha-sg",
        description="Sam security group for Pulumi deployment",
        ingress=[
            {"protocol": "TCP", "from_port": 22, "to_port": 22, 'cidr_blocks': ['0.0.0.0/0'], "description": "SSH"},
            {"protocol": "TCP", "from_port": 8787, "to_port": 8787, 'cidr_blocks': ['0.0.0.0/0'], "description": "RSW"},
            {"protocol": "TCP", "from_port": 2049, "to_port": 2049, 'cidr_blocks': ['0.0.0.0/0'], "description": "NSF"},
            {"protocol": "TCP", "from_port": 80, "to_port": 80, 'cidr_blocks': ['0.0.0.0/0'], "description": "HTTP"},
            {"protocol": "TCP", "from_port": 5432, "to_port": 5432, 'cidr_blocks': ['0.0.0.0/0'], "description": "POSTGRESQL"},
        ],
        egress=[
            {"protocol": "All", "from_port": -1, "to_port": -1, 'cidr_blocks': ['0.0.0.0/0'], "description": "Allow all outbout traffic"},
        ],
        tags=tags
    )
    
    # --------------------------------------------------------------------------
    # Stand up the servers
    # --------------------------------------------------------------------------
    rsw_server_1 = make_rsw_server(
        "1", 
        tags=tags | {"Name": "rsw-1"},
        key_pair=key_pair,
        vpc_group_ids=[rsw_security_group.id]
    )
    rsw_server_2 = make_rsw_server(
        "2", 
        tags=tags | {"Name": "rsw-1"},
        key_pair=key_pair,
        vpc_group_ids=[rsw_security_group.id]
    )

    # --------------------------------------------------------------------------
    # Create EFS.
    # --------------------------------------------------------------------------
    # Create a new file system.
    file_system = efs.FileSystem("efs-rsw-ha",tags= tags | {"Name": "rsw-ha-efs"})
    pulumi.export("efs_id", file_system.id)

    # Create a mount target. Assumes that the servers are on the same subnet id.
    mount_target = efs.MountTarget(
        f"mount-target-rsw",
        file_system_id=file_system.id,
        subnet_id=rsw_server_1.subnet_id,
        security_groups=[rsw_security_group.id]
    )
    
    # --------------------------------------------------------------------------
    # Create a postgresql database.
    # --------------------------------------------------------------------------
    db = rds.Instance(
        "rsw-db",
        instance_class="db.t3.micro",
        allocated_storage=5,
        username="rsw_db_admin",
        password="password",
        db_name="rsw",
        engine="postgres",
        publicly_accessible=True,
        skip_final_snapshot=True,
        tags=tags | {"Name": "rsw-db"},
        vpc_security_group_ids=[rsw_security_group.id]
    )
    pulumi.export("db_port", db.port)
    pulumi.export("db_address", db.address)
    pulumi.export("db_endpoint", db.endpoint)
    pulumi.export("db_name", db.name)
    pulumi.export("db_domain", db.domain)

    # --------------------------------------------------------------------------
    # Install required software one each server
    # --------------------------------------------------------------------------
    for name, server in zip(
        [1, 2],
        [rsw_server_1, rsw_server_2]
    ):
        connection = remote.ConnectionArgs(
            host=server.public_dns, 
            user="ubuntu", 
            private_key=Path("key.pem").read_text()
        )

        command_set_environment_variables = remote.Command(
            f"server-{name}-set-env", 
            create=pulumi.Output.concat(
                'echo "export EFS_ID=',            file_system.id,           '" >> .env;\n',
                'echo "export RSW_LICENSE=',       os.getenv("RSW_LICENSE"), '" >> .env;',
            ), 
            connection=connection, 
            opts=pulumi.ResourceOptions(depends_on=[server, db, file_system])
        )

        command_install_justfile = remote.Command(
            f"server-{name}-install-justfile",
            create="\n".join([
                """curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to ~/bin;""",
                """echo 'export PATH="$PATH:$HOME/bin"' >> ~/.bashrc;"""
            ]),
            connection=connection, 
            opts=pulumi.ResourceOptions(depends_on=[server])
        )

        command_copy_justfile = remote.CopyFile(
            f"server-{name}-copy-justfile",  
            local_path="server-side-files/justfile", 
            remote_path='justfile', 
            connection=connection, 
            opts=pulumi.ResourceOptions(depends_on=[server]),
            triggers=[hash_file("server-side-files/justfile")]
        )

        # Copy the server side files
        @dataclass
        class serverSideFile:
            file_in: str
            file_out: str
            template_render_command: pulumi.Output

        server_side_files = [
            serverSideFile(
                "server-side-files/config/database.conf",
                "~/database.conf",
                pulumi.Output.all(db.address).apply(lambda x: create_template("server-side-files/config/database.conf").render(db_address=x[0]))
            ),
            serverSideFile(
                "server-side-files/config/load-balancer",
                "~/load-balancer",
                pulumi.Output.all(server.public_ip).apply(lambda x: create_template("server-side-files/config/load-balancer").render(server_ip_address=x[0]))
            ),
            serverSideFile(
                "server-side-files/config/rserver.conf",
                "~/rserver.conf",
                pulumi.Output.all().apply(lambda x: create_template("server-side-files/config/rserver.conf").render())

            ),
        ]

        command_copy_config_files = []
        for f in server_side_files:
            command_copy_config_files.append(
                remote.Command(
                    f"copy {f.file_out} server {name}",
                    create=pulumi.Output.concat('echo "', f.template_render_command, f'" > {f.file_out}'),
                    connection=connection, 
                    opts=pulumi.ResourceOptions(depends_on=[server]),
                    triggers=[hash_file(f.file_in)]
                )
            )
        
        command_build_rsw = remote.Command(
            f"server-{name}-build-rsw", 
            # create="alias just='/home/ubuntu/bin/just'; just build-rsw", 
            create="""export PATH="$PATH:$HOME/bin"; just build-rsw""", 
            connection=connection, 
            opts=pulumi.ResourceOptions(depends_on=[command_set_environment_variables, command_install_justfile, command_copy_justfile] + command_copy_config_files)
        )


main()
