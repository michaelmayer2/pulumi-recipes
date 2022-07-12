# RStudio Workbench High availability setup.

![](infra.drawio.png)

## Usage

There are two primary files:

- `__main__.py`: contains the python code that will stand up the AWS resources.
- `templates/justfile`: contains the commands required to install RSW and the required dependencies. This file will be copied to each ec2 instance so that it can be executed on the server.

### Step 1: Create new virtual environment

```bash
python -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip wheel
pip install -r requirements.txt
```

### Step 2: Spin up infra

Create all of the infrastructure.

```bash
export AWS_SSH_KEY_ID="XXXXXX"
export AWS_PRIVATE_KEY_PATH="~/XXXX.pem"
pulumi up
```

### Step 3: Validate that RSW is working

Visit RSW in your browser:

```
just open
```

Login and start some new sessions.

You can also ssh into the ec2 instances for any debugging.

```bash
just ssh 1
```

```bash
just ssh 2
```

Check the status of the load balancer when you are sshed into the ec2 instance:

```bash
just status-load-balancer
```