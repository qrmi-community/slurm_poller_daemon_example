# slurm_license_poller(slp)

A poller daemon to update Slurm dynamic licenses based on the current status of IQP backends.

It provides a reference implementation of the Sensor pattern, in which the state of a quantum system is monitored and fed into Slurm's scheduling decisions through the Dynamic License mechanism. The sample demonstrates how Slurm scheduling can be coordinated with external quantum resource availability. When the quantum resource is occupied by another user, Slurm keeps the job in the PENDING state rather than allocating compute resources that would otherwise sit idle while waiting for quantum execution. 

Resources can be configured to require a license before a job may proceed; the scheduler checks license availability during the Schedule state. If the license cannot be acquired, the job remains pending until it becomes available.
Dynamic Licenses, available since Slurm v23, allow an external license manager to control the license count at runtime.

This mechanism enables the following workflow:

- Define one license per quantum backend. No changes to slurm.conf — no cluster restart required

```bash
sacctmgr add resource name=ibm_kingston \
    count=1 \
    cluster=<your cluster name(e.g. linux) \
    allowed=100 \
    type=license

sacctmgr -i update resource <your IQP backend, e.g. ibm_kingston> set lastconsumed=1

scontrol show license
sacctmgr show resource withcluster
```

- A daemon process runs on one node in the cluster, acting as an external license manager. It monitors the quantum backend and, when the backend becomes ready to execute jobs (i.e., its pending job count reaches zero), uses the sacctmgr CLI to increment the Dynamic License count from 1 to 0.

```bash
sacctmgr -i update resource ibm_kingston set lastconsumed=0
```

- Users add a license requirement to their sbatch invocations.

```bash
sbatch --licenses=ibm_kingston@slurmdb:1 run_sampler.sh
```

The Slurm scheduler checks the Dynamic License count and, when available, allocates GPU and other resources and transitions the job to the Execute state.


## Set up Python virtual development environment

Virtual environments are used for slp development to isolate the development environment
from system-wide packages. This way, we avoid inadvertently becoming dependent on a
particular system configuration. For developers, this also makes it easy to maintain multiple
environments (e.g. one per supported Python version, for older versions of slp, etc.).

### Set up a Python venv

All Python versions supported by Qiskit include built-in virtual environment module
[venv](https://docs.python.org/3/tutorial/venv.html).

Start by creating a new virtual environment with `venv`. The resulting
environment will use the same version of Python that created it and will not inherit installed
system-wide packages by default. The specified folder will be created and is used to hold the environment's
installation. It can be placed anywhere. For more detail, see the official Python documentation,
[Creation of virtual environments](https://docs.python.org/3/library/venv.html).

```
python3 -m venv ~/.venvs/slurm-license-poller
```

Activate the environment by invoking the appropriate activation script for your system, which can
be found within the environment folder. For example, for bash/zsh:


```
source ~/.venvs/slurm-license-poller/bin/activate
```

Upgrade pip within the environment to ensure the dependencies installed in the subsequent sections
can be located for your system.  You need `pip>=25.1` to use the `--group` feature used to manage
developer dependency groups.

```
pip install -U pip
```

You can easily install all the standard developer dependencies for in-place testing, documentation-building, and linting using:

```
pip install .
```

### Set up a Conda environment

For Conda users, a new environment can be created as follows.

```
conda create -y -n slurm_license_poller python=3
conda activate slurm_license_poller
```

```
pip install -e .
```

## Configuration([config.json](./config.json))

| Property | Default | Description |
|---|---|---|
| `$.api_token` | *(required)* | API Token to access IBM Quantum Platform |
| `$.service_crn` | *(required)* | Service CRN of your IBM Quantum Platform instance |
| `$.backends` | *(required)* | A list of quantum backends to be monitored |
| `$.poll_interval` | *(required)* | Polling interval of IBM Qiskit Runtime REST API calls in seconds |
| `$.cluster_name` | *(required)* | Slurm cluster where dynamic licenses are available |


## Starting the Server

```bash
usage: slurm-license-poller [-h] [--config CONFIG]

Slurm License Poller

options:
  -h, --help       show this help message and exit
  --config CONFIG
```

## Stopping the Server

Ctrl+C
