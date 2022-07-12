"""An AWS Python Pulumi program"""

import os
from pathlib import Path
from textwrap import dedent
from typing import Dict, Optional, List
import json
import subprocess

import pulumi
from pulumi_aws import ec2, efs, rds, ssm, iam
from pulumi_command import remote


def get_private_key(file_path: str) -> str:
    path = Path(file_path)
    if path.exists() == False:
        path = path.expanduser()
    with open(path, mode="r") as f:
        private_key = f.read()
    return private_key


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
    # Tags to apply to all resources.
    # --------------------------------------------------------------------------
    tags = {
        "rs:environment": "development",
        "rs:owner": "sam.edwardes@rstudio.com",
        "rs:project": "solutions",
    }

    # --------------------------------------------------------------------------
    # Set up keys.
    # --------------------------------------------------------------------------
    print(os.getenv("AWS_SSH_KEY_ID"))
    key_pair = ec2.get_key_pair(key_pair_id=os.getenv("AWS_SSH_KEY_ID"))
    private_key = get_private_key(os.getenv("AWS_PRIVATE_KEY_PATH"))
    
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
        tags=tags | {"Name": "samedwardes-rsw-1"},
        key_pair=key_pair,
        vpc_group_ids=[rsw_security_group.id]
    )
    rsw_server_2 = make_rsw_server(
        "2", 
        tags=tags | {"Name": "samedwardes-rsw-1"},
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
        tags=tags | {"Name": "samedwardes-rsw-db"},
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
            private_key=private_key
        )

        _set_env = remote.Command(
            f"server-{name}-set-env", 
            create=pulumi.Output.concat(
                'echo "export SERVER_IP_ADDRESS=', server.public_ip,         '" > .env;\n',
                'echo "export DB_ADDRESS=',        db.address,               '" >> .env;\n',
                'echo "export EFS_ID=',            file_system.id,           '" >> .env;\n',
                'echo "export RSW_LICENSE=',       os.getenv("RSW_LICENSE"), '" >> .env;',
            ), 
            connection=connection, 
            opts=pulumi.ResourceOptions(depends_on=[server, db, file_system])
        )

        _install_justfile = remote.Command(
            f"server-{name}-install-justfile",
            create="\n".join([
                """curl --proto '=https' --tlsv1.2 -sSf https://just.systems/install.sh | bash -s -- --to ~/bin;""",
                """echo 'export PATH="$PATH:$HOME/bin"' >> ~/.bashrc;"""
            ]),
            connection=connection, 
            opts=pulumi.ResourceOptions(depends_on=[server])
        )

        _copy_justfile = remote.CopyFile(
            f"server-{name}--copy-justfile",  
            local_path="templates/justfile", 
            remote_path='justfile', 
            connection=connection, 
            opts=pulumi.ResourceOptions(depends_on=[server])
        )
        
        _build_rsw = remote.Command(
            f"server-{name}-build-rsw", 
            # create="alias just='/home/ubuntu/bin/just'; just build-rsw", 
            create="""export PATH="$PATH:$HOME/bin"; just build-rsw""", 
            connection=connection, 
            opts=pulumi.ResourceOptions(depends_on=[_set_env, _install_justfile, _copy_justfile])
        )


main()
