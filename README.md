# slurm_license_poller (slp)

A poller daemon that updates Slurm dynamic licenses based on the current status of IBM Quantum Platform (IQP) backends.

It provides a reference implementation of the **Sensor pattern**: the state of a quantum system is monitored and fed into Slurm's scheduling decisions through Slurm's Dynamic License mechanism. This demonstrates how Slurm scheduling can be coordinated with external quantum resource availability — when a quantum backend is occupied by another user, Slurm keeps the job in the `PENDING` state instead of allocating compute resources that would otherwise sit idle while waiting for quantum execution.

## How it works

Slurm resources can be configured to require a license before a job may proceed. The scheduler checks license availability during the `Schedule` state; if the license can't be acquired, the job stays pending until it becomes available. **Dynamic Licenses** (available since Slurm v23) let an external license manager control the license count at runtime — which is exactly the role this daemon plays for quantum backends.

The workflow has three parts:

1. **One license is defined per quantum backend**, with no changes to `slurm.conf` and no cluster restart required:

   ```bash
   sacctmgr add resource name=ibm_kingston \
       count=1 \
       cluster=<your cluster name> \  # e.g. linux
       allowed=100 \
       type=license

   sacctmgr -i update resource ibm_kingston set lastconsumed=1

   scontrol show license
   sacctmgr show resource withcluster
   ```

2. **`slurm_license_poller` runs as a daemon on one node in the cluster**, acting as the external license manager. It monitors each configured quantum backend, and when a backend becomes ready to execute jobs (its pending job count reaches zero), it uses the `sacctmgr` CLI to release the license by setting the consumed count back to `0`:

   ```bash
   sacctmgr -i update resource ibm_kingston set lastconsumed=0
   ```

3. **Users request the license in their `sbatch` invocation**:

   ```bash
   sbatch --licenses=ibm_kingston@slurmdb:1 run_sampler.sh
   ```

   The Slurm scheduler checks the Dynamic License count and, once it's available, allocates GPU and other resources and transitions the job to the `Execute` state.

## (Optional) Job submit plugin (`plugins/`)

The mechanism above only works if users actually request the license on their `sbatch` command line. Nothing in Slurm stops a user from forgetting `--licenses=ibm_kingston@slurmdb:1` — in which case the job just runs immediately without waiting for the backend, defeating the purpose of the license.

To close that gap, the [`plugins/`](./plugins) directory contains a Slurm [Job Submit Plugin](https://slurm.schedmd.com/job_submit_plugins.html) that checks, at submission time, whether a job requests the required `--licenses` option. If it doesn't, the plugin rejects the job and returns an error prompting the user to add the option, rather than letting it run without coordinating with the quantum backend.

See the [Slurm documentation](https://slurm.schedmd.com/job_submit_plugins.html) for how to install and enable a `job_submit` plugin on your cluster.

## Installation

slp requires a Python virtual environment (venv or Conda), which isolates development from system-wide packages and makes it easy to maintain multiple environments — e.g. one per supported Python version.

### Using venv

All Python versions supported by Qiskit include the built-in [`venv`](https://docs.python.org/3/library/venv.html) module.

Create a new environment (this uses the Python version that created it and does not inherit system-wide packages by default; the target folder can be placed anywhere):

```bash
python3 -m venv ~/.venvs/slurm-license-poller
```

Activate it (bash/zsh shown; see the [venv docs](https://docs.python.org/3/tutorial/venv.html) for other shells):

```bash
source ~/.venvs/slurm-license-poller/bin/activate
```

Upgrade pip — `pip>=25.1` is required for the `--group` feature used to manage developer dependency groups:

```bash
pip install -U pip
```

Install slp along with the standard developer dependencies (testing, docs, linting):

```bash
pip install .
```

### Using Conda

```bash
conda create -y -n slurm_license_poller python=3
conda activate slurm_license_poller
pip install -e .
```

## Configuration ([config.json](./config.json))

| Property | Default | Description |
|---|---|---|
| `$.api_token` | *(required)* | API Token to access IBM Quantum Platform |
| `$.service_crn` | *(required)* | Service CRN of your IBM Quantum Platform instance |
| `$.backends` | *(required)* | A list of quantum backends to be monitored |
| `$.poll_interval` | *(required)* | Polling interval of IBM Qiskit Runtime REST API calls, in seconds |
| `$.cluster_name` | *(required)* | Slurm cluster where dynamic licenses are available |

## Usage

### Starting the server

```bash
usage: slurm-license-poller [-h] [--config CONFIG]

Slurm License Poller

options:
  -h, --help       show this help message and exit
  --config CONFIG
```

### Stopping the server

<kbd>Ctrl</kbd>+<kbd>C</kbd>
