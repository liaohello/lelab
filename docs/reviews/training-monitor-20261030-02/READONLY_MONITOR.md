# Read-only monitor for an existing remote run

This snapshot can attach the LeLab page to an already-running SmolVLA process.
The attach path tails the process's existing stdout log and checks its process
identity over SSH. It does not start, pause, resume, stop, or reconfigure the
remote trainer, and it does not write to the remote run directory.

## Local configuration

Set these variables only in the local shell or an ignored local environment
file. Do not put real values in tracked source or release documentation.

```bash
export LELAB_READONLY_SSH_TARGET='user@training-host'
export LELAB_READONLY_SSH_PORT='22'
export LELAB_READONLY_SSH_KEY='/path/to/private/ssh-key'
export LELAB_READONLY_REMOTE_LOG='/absolute/remote/path/to/run_stdout.log'
export LELAB_READONLY_REMOTE_PID='12345'
export LELAB_READONLY_TOTAL_STEPS='15000'
export LELAB_READONLY_JOB_NAME='SmolVLA · read-only monitor'
```

The SSH identity must already be authorized, and the remote host key must be
known to the local SSH client. The attach API validates that the log is
readable and the specified process still has `train_wrapper.py` in its command
line before it registers the run.

## Attach and open

Start the app with a local output root and bind it to loopback. Then attach once:

```bash
LELAB_OUTPUT_ROOT='/path/to/local/monitor-jobs' \
CUDA_VISIBLE_DEVICES='' \
python -m uvicorn lelab.server:app --host 127.0.0.1 --port 18080

curl -fsS -X POST http://127.0.0.1:18080/jobs/attach-readonly
```

Open `/training/<id>` using the `id` returned by the attach endpoint. The job
appears in the ordinary LeLab training list and keeps locally persisted log and
metric history. Existing log lines are parsed for tqdm progress and any
`step/loss/lr/grdn` metrics the trainer emitted. Validation loss, weight norm,
and grouped SmolVLA losses are shown only if present in the source log; this
legacy run does not gain structured events retroactively.

The monitor page labels the job **Remote · read-only** and hides training
controls. If the SSH observer disconnects or LeLab restarts, the training
process is left alone; attach again to resume observation.
