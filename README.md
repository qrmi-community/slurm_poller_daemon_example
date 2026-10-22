# slurm_license_poller (slp)

A poller daemon that updates Slurm dynamic license counts based on the current status of quantum resources, as reported by [QRMI](https://github.com/qiskit-community/qrmi) (Quantum Resource Management Interface).

It provides a reference implementation of the **Sensor pattern**: the state of a quantum system is monitored and fed into Slurm's scheduling decisions through [Slurm's Dynamic License mechanism](https://slurm.schedmd.com/licenses.html#dynamic_licenses). This demonstrates how Slurm scheduling can be coordinated with external quantum resource availability — when a quantum backend is occupied by another user, Slurm keeps the job in the `PENDING` state instead of allocating compute resources that would otherwise sit idle while waiting for quantum execution.

Any resource type supported by QRMI can be monitored (e.g. IBM Quantum Compute Service, IBM Quantum System Service, Pasqal Cloud, IQM Server); the resources themselves are defined in a QRMI config file.


## How it works

Slurm resources can be configured to require a license before a job may proceed. The scheduler checks license availability during the `Schedule` state; if the license can't be acquired, the job stays pending until it becomes available. **Dynamic Licenses** (available since Slurm v23.03) let an external license manager control the license count at runtime — which is exactly the role this daemon plays for quantum backends.

The workflow has three parts:

1. **One license is defined per quantum backend**, with no changes to `slurm.conf` and no cluster restart required. The license name must be identical to the resource ID used in the QRMI config file and in this daemon's `resources` list:

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

   With Slurm 25.05.3 we saw `slurmctld` crash in the backfill scheduler when a pending job requested a license added after `slurmctld` started. Run `scontrol reconfigure` after adding a license.

2. **`slurm_license_poller` runs as a daemon on one node in the cluster**, acting as the external license manager. Every `poll_interval` seconds it queries the status of each configured resource through QRMI and uses the `sacctmgr` CLI to update the license:

   ```bash
   # Quantum backend is READY to accept the jobs
   sacctmgr -i update resource ibm_kingston set lastconsumed=0

   # Quantum backend is BUSY (or unavailable)
   sacctmgr -i update resource ibm_kingston set lastconsumed=1
   ```

   A backend is considered **ready** only if QRMI reports it as `online` and it does not report itself as unhealthy (`healthy == false`) or busy (`busy == true`). Vendors that don't report `healthy`/`busy` are treated as ready whenever they are online. `offline` and `paused` backends are treated as busy.

   The daemon reads each license's count from Slurm at startup, so the licenses must exist before it starts. A busy backend consumes the whole count. A license with a count above 1 can also track the capacity QRMI reports: if a ready backend has `available_slots` free, the daemon sets `lastconsumed = count - available_slots`. See [Pasqal Warden QPU slots](#pasqal-warden-qpu-slots). A license with count 1 remains a busy/ready flag.

   The daemon is designed to **fail closed**, so jobs never run against a backend whose state is unknown:

   - If the status of a backend can't be retrieved `failure_threshold` times in a row (e.g. network or authentication errors), its license is marked fully consumed until the status is available again.
   - On shutdown (`SIGTERM` / `SIGINT`), every license is marked fully consumed, since nothing maintains it any more. Restarting the daemon releases the licenses of ready backends on its first poll.
   - The license is written whenever the observed state changes, and re-written every `resync_interval` seconds even if it hasn't, so manual edits or a restored `slurmdbd` are corrected.

3. **Users request the license in their `sbatch` invocation**:

   ```bash
   sbatch --licenses=ibm_kingston@slurmdb:1 run_sampler.sh
   ```

   The Slurm scheduler checks the Dynamic License count and, once it's available, allocates GPU and other resources and transitions the job to the `Execute` state.

## Pasqal Warden QPU slots

A [Pasqal](https://www.pasqal.com/) on-premises QPU is served by one Warden instance and configured in QRMI as a `pasqal-local` resource. Warden splits the QPU into QPU slots, and QRMI reports them as `capacity` (`max_slots`, `available_slots`). A job reserves its slots when the [QRMI SPANK plugin](https://github.com/qiskit-community/spank-plugins) opens its Warden session. Warden maintenance is reported as `paused`.

Give the license the QPU's resource ID and the same count as Warden's `qpu_slots_total`. `sacctmgr` stores license names in lowercase and matches them case-insensitively, so the daemon can use the ID `PASQAL_LOCAL`, but jobs must request `pasqal_local`:

```bash
sacctmgr -i add resource name=PASQAL_LOCAL server=slurmdb type=license \
    count=10 flags=absolute cluster=<your cluster name> allowed=10
sacctmgr -i update resource PASQAL_LOCAL set lastconsumed=10
```

The QRMI config entry needs only the Warden URL. The job identity (`QRMI_JOB_UID`, `QRMI_JOB_ID`) is only needed to open a session, which the daemon never does:

```json
{"name": "PASQAL_LOCAL", "type": "pasqal-local",
 "environment": {"QRMI_WARDEN_URL": "http://warden-host:4207"}}
```

With `"resources": ["PASQAL_LOCAL"]`, the daemon publishes the slots in use, including those held outside Slurm, while Warden is accessible. During maintenance, on errors, and on shutdown it publishes all 10. Slurm subtracts the slots of its own running jobs from `lastconsumed`, so jobs needing more slots than are free stay `PENDING (Licenses)`. Each QPU has its own Warden, resource and license, so a failure consumes only that QPU's license.

Request one license per slot, and set the same count in `PASQAL_LOCAL_QRMI_JOB_QPU_SLOTS` (default 1). The SPANK plugin passes the job's `{resource}_QRMI_*` variables to QRMI, which claims that many slots when it opens the Warden session:

```bash
export PASQAL_LOCAL_QRMI_JOB_QPU_SLOTS=4
sbatch --qpu=PASQAL_LOCAL --licenses=pasqal_local@slurmdb:4 job.sh
```

The license only gates admission. The Warden session opened by the SPANK plugin still decides whether the slots are granted. The job submit plugin below rejects batch jobs whose license count and `{resource}_QRMI_JOB_QPU_SLOTS` differ.

## (Optional) Job submit plugin (`plugins/`)

> [!NOTE]
> This plugin code works with [QRMI SPANK plugin 0.12.0](https://github.com/qiskit-community/spank-plugins/releases/tag/0.12.0) or above.

The mechanism above only works if users actually request the license on their `sbatch` command line. Nothing in Slurm stops a user from forgetting `--licenses=ibm_kingston@slurmdb:1` — in which case the job just runs immediately without waiting for the backend, defeating the purpose of the license.

To close that gap, the [`plugins/`](./plugins) directory contains a Slurm [Job Submit Plugin](https://slurm.schedmd.com/job_submit_plugins.html) that checks, at submission time, whether a job requests the required `--licenses` option. If it doesn't, the plugin rejects the job and returns an error prompting the user to add the option, rather than letting it run without coordinating with the quantum backend. For batch jobs it also rejects a license count that differs from `{qpu}_QRMI_JOB_QPU_SLOTS` (default 1); `srun` and `salloc` [don't send their environment](https://slurm.schedmd.com/job_submit_plugins.html#lua) to `slurmctld`, so it can't check theirs.

Slurm stores license names in lowercase, so the plugin expects the lowercase form of the `--qpu` name: a job with `--qpu=EMU_FREE` requests `--licenses=emu_free@slurmdb:1`.

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

Upgrade pip and install slp:

```bash
pip install -U pip
cd poller
pip install .
```

To also install the developer tools (pytest, black, ruff) and run the tests:

```bash
pip install -e ".[dev]"
pytest
```

### Using Conda

```bash
conda create -y -n slurm_license_poller python=3
conda activate slurm_license_poller
cd poller
pip install -e .
```

## Configuration

| Property | Default | Description |
|---|---|---|
| `$.config_path` | *(required)* | Path to the QRMI config file that defines the quantum resources (endpoints, credentials, etc.) |
| `$.resources` | *(required)* | Non-empty list of QRMI resource IDs to monitor. Each ID is also used as the Slurm license name |
| `$.poll_interval` | *(required)* | Polling interval, in seconds (> 0) |
| `$.failure_threshold` | `3` | Number of consecutive status failures after which a backend's license is marked consumed |
| `$.resync_interval` | `300` | Interval, in seconds, at which an unchanged license is re-written to Slurm |
| `$.sacctmgr_timeout` | `30` | Timeout, in seconds, for each `sacctmgr` invocation |
| `$.log_level` | `"INFO"` | Log level. Overridden by `--log-level`; ignored if a log config is used |
| `$.log_config` | *(none)* | Path to a JSON `logging.config.dictConfig` file. Overridden by `--log-config` |

Unknown keys are ignored with a warning. See [config.json.example](./poller/config.json.example) for an example.


## Logging

This program uses python standard logger. You can configure with [Configuration dictionary file](https://docs.python.org/3/library/logging.config.html#logging-config-dictschema). [An example](./poller/log_config.json.example) is available for your reference.


## Usage

### Starting the server

```bash
usage: slurm-license-poller [-h] [--config CONFIG] [--log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}] [--log-config LOG_CONFIG]

Slurm License Poller

options:
  -h, --help            show this help message and exit
  --config CONFIG       config json file
  --log-level {DEBUG,INFO,WARNING,ERROR,CRITICAL}
                        Overrides the config file's log_level (default: INFO). Ignored if --log-config or the config's log_config is set.
  --log-config LOG_CONFIG
                        Path to a JSON logging.config.dictConfig file. Overrides --log-level and the config file's log_level entirely.
```

### Stopping the server

Press <kbd>Ctrl</kbd>+<kbd>C</kbd> or send `SIGTERM` (e.g. `systemctl stop`). The daemon finishes the backend it is currently polling, marks every license as consumed, and exits. A second signal aborts immediately.
