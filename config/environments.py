# =============================================================================
# environments.py
# -----------------------------------------------------------------------------
# Responsible for: Running shell commands on behalf of the agent, either inside
#                  a Docker container (DockerEnvironment) or on the host
#                  (LocalEnvironment), and managing the container lifecycle.
# Role in project: Injected into Agent (Step 2); Agent.execute_action delegates
#                  to env.execute(). run_single.py (Step 3) owns start()/cleanup().
# Contract:        execute() returns {"output": str, "returncode": int}. On a
#                  timeout it raises subprocess.TimeoutExpired (with partial
#                  output). It never builds LM chat messages; Agent translates.
# Assumptions:     `docker` CLI on PATH with a running daemon (DockerEnvironment).
#                  The image provides bash, coreutils `timeout`, and `sleep`.
#                  `bash` on PATH for LocalEnvironment (macOS / Linux / WSL).
# =============================================================================

import os
import subprocess
import uuid
from typing import Protocol

# Exit code GNU `timeout` uses when the time limit is hit and TERM sufficed.
TIMEOUT_EXIT_CODE = 124


def swebench_image_name(instance_id: str) -> str:
    """
    Build the Docker Hub image name for a SWE-bench instance.

    Args:
        instance_id: SWE-bench id such as "sympy__sympy-15599".

    Returns:
        Full image reference, e.g.
        "docker.io/swebench/sweb.eval.x86_64.sympy_1776_sympy-15599:latest".

    Why:
        SWE-bench publishes one prebuilt image per instance under a fixed naming
        scheme; "__" is not allowed in Docker repo names, so it is encoded as
        "_1776_". Centralizing the rule means one place to fix if it changes.
    """
    formatted_instance_id = instance_id.replace("__", "_1776_")
    image_reference = f"docker.io/swebench/sweb.eval.x86_64.{formatted_instance_id}:latest"
    return image_reference.lower()


def remove_image(image: str) -> bool:
    """
    Remove one Docker image from the local cache. Never raises.

    Args:
        image: Image reference, e.g. from swebench_image_name().

    Returns:
        True if Docker removed it, False otherwise (a warning is printed).

    Why:
        A full 500-instance run cannot keep every image (Step 9 plan). Lives
        here because this module owns every docker call. Never raises, like
        cleanup(): a failed removal costs disk, not correctness, and must not
        stop a batch. Returns a bool so run_batch can count failures for the
        end-of-run summary.
    """

    try:
        result = subprocess.run(
            ["docker", "rmi", image], capture_output=True,
            text=True, timeout=120, check = False
        )

    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Timeout or OSError occurred from {image} due to {e} resulting in image left on disk")
        return False

    if result.returncode != 0:
        print(f"Docker image removal {image} failed at exit {result.returncode} resulting in image left on disk: {result.stderr.strip()}")
        return False

    return True


def image_exists(image: str) -> bool | None:
    """
    Check whether an image is in the local Docker cache, without pulling it.

    Args:
        image: Image reference, e.g. from swebench_image_name().

    Returns:
        True if the image is on disk, False if it is not, None if Docker could
        not be asked (timeout or docker not found).

    Why:
        A resumed batch removes the images an interrupted session left behind,
        but only those still on disk: calling remove_image on an absent image
        would print a warning and count a false removal failure. None is kept
        apart from False, so "could not check" is never read as "not there".
        `docker image inspect` accepts the docker.io/ prefix that
        swebench_image_name adds, unlike matching the `docker images` listing
        (about 30 ms per call, so checking all 500 ids takes about 15 s).
    """

    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, text=True,
            timeout=30, check = False
        )

    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"Image {image} could not be verified: {e}")
        return None

    if result.returncode != 0:
        if "No such image" in result.stderr:
            return False

        print(f"Image {image} could not be checked: {result.stderr.strip()}")
        return None

    return True

class Environment(Protocol):
    """
    Structural interface every environment satisfies (no inheritance needed).

    Why:
        Lets Agent (Step 2) type-hint `env: Environment` and accept Docker or
        Local interchangeably. Optional: delete if you prefer no Protocol yet.
    """

    def start(self) -> None: ...
    def execute(self, command: str) -> dict: ...
    def cleanup(self) -> None: ...

class DockerEnvironment:
    """Runs each command via `docker exec` in a container this object owns."""

    def __init__(
        self,
        image: str,
        cwd: str = "/testbed",
        timeout: int = 60,
        env: dict[str, str] | None = None,
        kill_after: int = 5,
        host_grace: int = 10,
        start_timeout: int = 600,
        name_prefix: str = "minimal-agent",
    ) -> None:
        """
        Store configuration only; does not touch Docker.

        Args:
            image: Image to run (e.g. from swebench_image_name()).
            cwd: Working directory inside the container for every command.
            timeout: Seconds a single command may run inside the container.
            env: Extra env vars passed to each `docker exec` (-e KEY=VALUE).
            kill_after: Seconds after TERM before `timeout` sends KILL.
            host_grace: Extra seconds the host waits beyond timeout + kill_after
                before giving up on the `docker exec` client itself.
            start_timeout: Seconds allowed for `docker run` (includes a first-time
                image pull, which can take minutes).
            name_prefix: Prefix for the generated unique container name.

        Returns:
            None.

        Why:
            Separating construction from side effects keeps objects cheap to
            build and test, and makes start() the single place setup can fail.
        """
        self.image = image
        self.cwd = cwd
        self.timeout = timeout
        self.kill_after = kill_after
        self.host_grace = host_grace
        self.start_timeout = start_timeout
        self.name_prefix = name_prefix

        self.env = {} if env is None else dict(env)

        self.container_name: str | None = None
        self.container_id: str | None = None

    def start(self) -> None:
        """
        Start a detached, long-running container and record its name and ID.

        Args:
            None.

        Returns:
            None. Sets self.container_name and self.container_id.

        Raises:
            RuntimeError: if already started, or if `docker run` fails or times out.

        Why:
            The container must outlive individual commands so file edits persist
            across steps; a no-op long-running process keeps it alive.
        """
        if self.container_id is not None:
            raise RuntimeError("Container already started.")

        self.container_name = f"{self.name_prefix}-{uuid.uuid4().hex[:8]}"

        # Platform hardcorded as SWE-bench images are x86_64 only,
        # run under emulated using Apple Silicon.

        argv = [
            "docker", "run",
            "-d", "--platform",
            "linux/amd64", "--name",
            self.container_name,
            "-w", self.cwd,
            self.image,
            "sleep", "infinity",
        ]

        try:

            # docker run -d --platform linux/amd64 --name swe-test \
            #  docker.io/swebench/sweb.eval.x86_64.sympy_1776_sympy-15599:latest sleep infinity

            result = subprocess.run(
                argv,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.start_timeout,
                check=False,
            )

        except subprocess.TimeoutExpired as e:

            raise RuntimeError(f"Docker run did not finish within {self.start_timeout}s") from e

        if result.returncode != 0:
            raise RuntimeError(result.stderr)


        self.container_id = result.stdout.strip()

    def _docker_exec_argv(self, command: str) -> list[str]:
        """
        Build the full `docker exec` argv for one command.

        Args:
            command: The bash command string from the model.

        Returns:
            List-form argv suitable for subprocess.run without shell=True.

        Why:
            Pure function of config + command, so it can be unit-tested without
            Docker. The in-container `timeout` is what actually kills a runaway
            process; killing the host-side `docker exec` client does not.
        """
        if self.container_id is None:
            raise RuntimeError("Container not started, call start() first.")

        argv = [
            "docker", "exec",
            "-w", self.cwd
        ]
        for key, value in self.env.items():
            argv.extend(["-e", f"{key}={value}"])

        argv.extend([self.container_id, "timeout", f"--kill-after={self.kill_after}", str(self.timeout)])
        argv.extend(["bash", "-c", command])

        return argv

    def execute(self, command: str) -> dict:
        """
        Run one command in the container and return its combined output.

        Args:
            command: Bash command string to run.

        Returns:
            {"output": str, "returncode": int}, with stderr merged into output.

        Raises:
            RuntimeError: if start() has not been called.
            subprocess.TimeoutExpired: if the command hit the in-container limit
                (exit code 124) or the host-side backstop fired.

        Why:
            Same dict shape as the old execute_action so format_output is
            unchanged. Both timeout paths are unified into one exception type
            so the Agent has a single thing to catch.
        """
        result = subprocess.run(
            self._docker_exec_argv(command),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.timeout + self.kill_after + self.host_grace,
            check=False,
        )

        # Error 137 (OOM event) is being deliberately ignored as it is
        #  ambigious with an OOM kill or kill -9 error.

        if result.returncode == TIMEOUT_EXIT_CODE:
            raise subprocess.TimeoutExpired(cmd=command, timeout=self.timeout, output=result.stdout)

        return {"output": result.stdout, "returncode": result.returncode}

    def cleanup(self) -> None:
        """
        Force-remove the container. Safe to call repeatedly or after a failed start.

        Args:
            None.

        Returns:
            None. Never raises.

        Why:
            Runs inside `finally` in run_single.py; raising here would mask the
            original exception. Removal is by name, which exists even when
            `docker run` never returned an ID.
        """
        if self.container_name is None:
            return

        try:

            result = subprocess.run([
                "docker", "rm",
                "-f", self.container_name],
                capture_output=True, timeout = 60,
                text=True, check=False
            )

        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"Failed to handle {self.container_name}: {e!r}")
            return

        if result.returncode != 0:
            print(f"{self.container_name} may need manual rm {result.stderr.strip()}")
            return

        self.container_name = None
        self.container_id = None


class LocalEnvironment:
    """Runs each command in a host bash subprocess. Same interface as Docker."""

    def __init__(
        self,
        cwd: str | None = None,
        timeout: int = 60,
        env: dict[str, str] | None = None,
    ) -> None:
        """
        Store configuration only.

        Args:
            cwd: Working directory for commands; None means the current directory.
            timeout: Seconds a single command may run.
            env: Extra env vars layered over the host environment.

        Returns:
            None.

        Why:
            Lets you exercise the agent loop without Docker (fast local debugging).
        """
        self.cwd = cwd
        self.timeout = timeout
        self.env = {} if env is None else dict(env)

    def start(self) -> None:
        """
        No-op; present so callers can treat every environment identically.

        Returns:
            None.

        Why:
            run_single.py calls start()/cleanup() unconditionally.
        """

    def execute(self, command: str) -> dict:
        """
        Run one command with host bash and return its combined output.

        Args:
            command: Bash command string to run.

        Returns:
            {"output": str, "returncode": int}, with stderr merged into output.

        Raises:
            subprocess.TimeoutExpired: if the command exceeds self.timeout.

        Why:
            Mirrors DockerEnvironment.execute so Agent needs no branching.
            Known limitation: on timeout, grandchild processes may survive.
        """
        merged_env = {**os.environ, **self.env}

        result = subprocess.run(
            ["bash", "-c", command],
            cwd=self.cwd,
            env=merged_env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=self.timeout,
            check=False
        )

        return {"output": result.stdout, "returncode": result.returncode}

    def cleanup(self) -> None:
        """
        No-op counterpart to start().

        Returns:
            None.

        Why:
            Uniform lifecycle across environments.
        """
