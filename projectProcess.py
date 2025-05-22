# main.py
import argparse
import json
import logging
import os
import pathlib
import shutil
import subprocess
import time
import docker # type: ignore

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(message)s',
                    handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

# --- Configuration ---
# Base Docker image for running atom tools
ATOM_DOCKER_IMAGE = "ghcr.io/appthreat/atom:latest"
# Native image download URL (assuming Linux amd64, adjust if necessary)
ATOM_NATIVE_IMAGE_URL_LINUX = "https://github.com/AppThreat/atom/releases/latest/download/atom-amd64"
ATOM_NATIVE_IMAGE_NAME_LINUX = "atom-amd64"
ATOM_NATIVE_EXECUTABLE_NAME = "atom-native" # Name it will be moved to in /usr/local/bin

class ProjectProcessor:
    """
    Handles the processing of a single project: cloning, running atom (JAR & Native),
    and comparing outputs.
    """
    def __init__(self, project_config_path: pathlib.Path,
                 base_input_dir: pathlib.Path,
                 base_workspace_dir: pathlib.Path,
                 base_output_dir: pathlib.Path,
                 docker_client):
        self.project_config_path = project_config_path
        self.base_input_dir = base_input_dir
        self.base_workspace_dir = base_workspace_dir
        self.base_output_dir = base_output_dir
        self.docker_client = docker_client

        self.project_lang = project_config_path.parent.parent.name
        self.project_name = project_config_path.parent.name
        
        self.config = self._load_config()
        if not self.config:
            raise ValueError(f"Failed to load or validate config: {project_config_path}")

        self.project_clone_path = self.base_workspace_dir / self.project_lang / self.project_name / "source"
        self.project_output_path = self.base_output_dir / self.project_lang / self.project_name
        self.jar_output_dir = self.project_output_path / "jar_output"
        self.native_output_dir = self.project_output_path / "native_output"
        self.diff_dir = self.project_output_path / "diff_results"

        self.container_name = f"atom_processor_{self.project_lang.lower()}_{self.project_name.lower()}_{int(time.time())}"
        self.container = None
        self.tools_installed_in_container = False


    def _load_config(self) -> dict | None:
        """Loads and validates the project configuration JSON file."""
        try:
            with open(self.project_config_path, 'r') as f:
                config_data = json.load(f)
            
            # Basic validation
            if not config_data.get("github_url") or not config_data.get("language"):
                logger.error(f"Config {self.project_config_path} missing github_url or language.")
                return None
            if config_data["language"].lower() != self.project_lang.lower():
                logger.warning(f"Language in config ({config_data['language']}) "
                               f"differs from directory structure ({self.project_lang}). Using directory structure.")
            
            config_data["language"] = self.project_lang # Standardize based on folder
            
            if "atom_operations" not in config_data or not isinstance(config_data["atom_operations"], list):
                logger.error(f"Config {self.project_config_path} missing 'atom_operations' list.")
                return None
            
            return config_data
        except FileNotFoundError:
            logger.error(f"Project config file not found: {self.project_config_path}")
            return None
        except json.JSONDecodeError:
            logger.error(f"Error decoding JSON from {self.project_config_path}")
            return None

    def _run_host_command(self, command_parts: list[str], cwd: pathlib.Path | str | None = None, shell=False) -> bool:
        """Runs a command on the host system."""
        logger.info(f"Host CMD (cwd: {cwd}): {' '.join(command_parts)}")
        try:
            process = subprocess.run(command_parts, capture_output=True, text=True, check=False, cwd=cwd, shell=shell)
            if process.returncode != 0:
                logger.error(f"Host command failed (ret: {process.returncode}): {' '.join(command_parts)}")
                logger.error(f"Stdout: {process.stdout}")
                logger.error(f"Stderr: {process.stderr}")
                return False
            logger.info(f"Host command success. Stdout: {process.stdout[:200]}...")
            return True
        except Exception as e:
            logger.error(f"Exception running host command {' '.join(command_parts)}: {e}")
            return False

    def _clone_repo(self) -> bool:
        """Clones the project's GitHub repository."""
        logger.info(f"Cloning {self.config['github_url']} into {self.project_clone_path}...")
        if self.project_clone_path.exists():
            logger.info(f"Clone path {self.project_clone_path} already exists. Skipping clone.")
            # Potentially add a 'force_clone' or 'pull' option here
            return True
        
        self.project_clone_path.mkdir(parents=True, exist_ok=True)
        
        # Run pre-clone host commands if any
        for cmd_str in self.config.get("host_pre_clone_commands", []):
            if not self._run_host_command(cmd_str.split(), cwd=self.project_clone_path.parent): return False

        if not self._run_host_command(["git", "clone", self.config["github_url"], str(self.project_clone_path.name)], cwd=self.project_clone_path.parent):
            return False
        
        # Run post-clone host commands if any (e.g., git submodule update)
        for cmd_str in self.config.get("host_post_clone_commands", []):
            if not self._run_host_command(cmd_str.split(), cwd=self.project_clone_path): return False
            
        return True

    def _start_container(self) -> bool:
        """Starts a Docker container for processing."""
        if not self.project_clone_path.exists() or not any(self.project_clone_path.iterdir()):
             logger.error(f"Project source directory {self.project_clone_path} is empty or does not exist. Cannot start container.")
             return False

        logger.info(f"Starting Docker container '{self.container_name}' from image '{ATOM_DOCKER_IMAGE}'...")
        try:
            # Ensure the Docker image is pulled
            try:
                self.docker_client.images.get(ATOM_DOCKER_IMAGE)
                logger.info(f"Image {ATOM_DOCKER_IMAGE} found locally.")
            except docker.errors.ImageNotFound:
                logger.info(f"Image {ATOM_DOCKER_IMAGE} not found locally. Pulling...")
                self.docker_client.images.pull(ATOM_DOCKER_IMAGE)
                logger.info(f"Image {ATOM_DOCKER_IMAGE} pulled successfully.")

            self.container = self.docker_client.containers.run(
                ATOM_DOCKER_IMAGE,
                name=self.container_name,
                volumes={str(self.project_clone_path.resolve()): {'bind': '/app', 'mode': 'rw'}},
                working_dir='/app',
                command="sleep infinity", # Keep container running
                detach=True,
                auto_remove=False # Keep it for exec, will remove manually
            )
            # try:
            #     self.container.exec_run("curl -LO https://github.com/AppThreat/atom/releases/latest/download/atom-amd64")
            #     self.container.exec_run("chmod +x atom-amd64")
            #     exec_result = self.container.exec_run("./atom-amd64 --help")

            # except docker.errors.DockerException as e:
            #     logger.error(f"exec_run {e}")
            # Wait a moment for the container to be fully up
            time.sleep(5) 
            logger.info(f"Container '{self.container_name}' started with ID: {self.container.id}")
            return True
        except docker.errors.APIError as e:
            logger.error(f"Docker API error starting container: {e}")
            if "409" in str(e) and "Conflict" in str(e): # Container already exists
                logger.warning(f"Container {self.container_name} already exists. Attempting to use it.")
                try:
                    self.container = self.docker_client.containers.get(self.container_name)
                    if self.container.status != "running":
                        self.container.start()
                        time.sleep(5)
                    logger.info(f"Reattached to existing container {self.container_name}")
                    return True
                except docker.errors.NotFound:
                    logger.error(f"Could not reattach to container {self.container_name} after conflict.")
                    return False
            return False

    def _exec_in_container(self, command: str | list[str], workdir: str = "/app", user: str = "", environment: dict = None) -> tuple[int, str, str]:
        """Executes a command inside the running Docker container."""
        if not self.container:
            logger.error("Container not started. Cannot execute command.")
            return -1, "", "Container not started"

        if isinstance(command, list):
            command_str = " ".join(command) # For logging
            cmd_to_exec = command
        else: # If it's a single string, execute with sh -c for shell features
            command_str = command
            cmd_to_exec = ["sh", "-c", command]

        logger.info(f"Container EXEC (workdir: {workdir}, user: {user}): {command_str}")
        
        try:
            # Ensure container is running
            self.container.reload()
            if self.container.status != "running":
                logger.warning(f"Container {self.container_name} was not running. Attempting to start.")
                self.container.start()
                time.sleep(3) # Give it a moment
                self.container.reload()
                if self.container.status != "running":
                    logger.error(f"Failed to restart container {self.container_name}. Cannot exec.")
                    return -1, "", "Container not running"

            exit_code, (stdout_bytes, stderr_bytes) = self.container.exec_run(
                cmd_to_exec,
                workdir=workdir,
                user=user,
                environment=environment or {}
            )
            stdout = stdout_bytes.decode('utf-8', errors='replace') if stdout_bytes else ""
            stderr = stderr_bytes.decode('utf-8', errors='replace') if stderr_bytes else ""

            if exit_code != 0:
                logger.warning(f"Container command failed (ret: {exit_code}): {command_str}")
                logger.warning(f"Stdout: {stdout}")
                logger.warning(f"Stderr: {stderr}")
            else:
                logger.info(f"Container command success. Stdout: {stdout[:200]}...")
            return exit_code, stdout, stderr
        except docker.errors.APIError as e:
            logger.error(f"Docker API error during exec: {e}")
            return -1, "", str(e)
        except Exception as e:
            logger.error(f"Unexpected error during exec: {e}")
            return -1, "", str(e)

    def _install_atom_tools_in_container(self) -> bool:
        """Installs atom (npm), native image, cdxgen, and parsetools in the container."""
        if self.tools_installed_in_container:
            logger.info("Tools already reported as installed in this container session.")
            return True

        logger.info("Installing atom tools in container...")
        
        # 1. npm packages
        npm_packages = "@appthreat/atom @cyclonedx/cdxgen --omit=optional @appthreat/atom-parsetools"
        exit_code, _, _ = self._exec_in_container(f"npm install -g {npm_packages}")
        if exit_code != 0:
            logger.error("Failed to install npm packages in container.")
            return False
        logger.info("npm packages installed.")

        # 2. Atom native image (Linux amd64 assumed)
        # Check if already exists to avoid re-downloading if container is reused somehow
        exit_code, stdout, _ = self._exec_in_container(f"command -v {ATOM_NATIVE_EXECUTABLE_NAME}")
        if exit_code == 0 and ATOM_NATIVE_EXECUTABLE_NAME in stdout:
            logger.info(f"Atom native image '{ATOM_NATIVE_EXECUTABLE_NAME}' already found in container's PATH.")
        else:
            logger.info(f"Downloading and installing atom native image '{ATOM_NATIVE_IMAGE_NAME_LINUX}'...")
            native_install_cmds = [
                f"curl -fsSL -o {ATOM_NATIVE_IMAGE_NAME_LINUX} {ATOM_NATIVE_IMAGE_URL_LINUX}",
                f"chmod +x {ATOM_NATIVE_IMAGE_NAME_LINUX}",
                f"mv {ATOM_NATIVE_IMAGE_NAME_LINUX} /usr/local/bin/{ATOM_NATIVE_EXECUTABLE_NAME}",
                f"{ATOM_NATIVE_EXECUTABLE_NAME} --version" # Test it
            ]
            for cmd in native_install_cmds:
                exit_code, _, _ = self._exec_in_container(cmd)
                if exit_code != 0:
                    logger.error(f"Failed to install atom native image (command: {cmd}).")
                    return False
            logger.info("Atom native image installed.")
        
        self.tools_installed_in_container = True
        return True

    def _run_project_install_build_in_container(self) -> bool:
        """Runs project-specific installation and build commands inside the container."""
        logger.info("Running project install/build commands in container...")
        project_subdir = self.config.get("project_dir_in_repo", ".")
        workdir_path = f"/app/{project_subdir}" if project_subdir != "." else "/app"

        for cmd_str in self.config.get("install_commands_container", []):
            exit_code, _, _ = self._exec_in_container(cmd_str, workdir=workdir_path)
            if exit_code != 0:
                logger.error(f"Project install command failed: {cmd_str}")
                return False
        
        for cmd_str in self.config.get("build_commands_container", []):
            exit_code, _, _ = self._exec_in_container(cmd_str, workdir=workdir_path)
            if exit_code != 0:
                logger.error(f"Project build command failed: {cmd_str}")
                return False
        
        logger.info("Project install/build commands completed.")
        return True

    def _run_atom_operations(self, atom_executable: str, output_subdir: pathlib.Path) -> bool:
        """Runs the configured atom operations using the specified atom executable."""
        logger.info(f"Running atom operations using '{atom_executable}' for output to '{output_subdir}'...")
        output_subdir.mkdir(parents=True, exist_ok=True)
        
        project_lang = self.config["language"]
        project_source_container_path = self.config.get("project_dir_in_repo", ".")
        
        # Ensure cdxgen runs if reachables are planned (as per atom docs)
        # This is a heuristic. A more robust way would be to have explicit cdxgen steps in config.
        has_reachables = any(op.get("atom_main_command") == "reachables" for op in self.config.get("atom_operations", []))
        if has_reachables:
            logger.info("Reachables operation detected, ensuring SBOM generation with cdxgen...")
            # cdxgen output is typically bom.json or bom.xml in the project root
            # The -o . means output to current dir (/app/project_subdir)
            cdxgen_cmd = f"cdxgen -o bom.json --project-path ." 
            exit_code, _, _ = self._exec_in_container(cdxgen_cmd, workdir=f"/app/{project_source_container_path}")
            if exit_code != 0:
                logger.warning(f"cdxgen command failed. Reachables analysis might be affected.")
            else:
                logger.info("cdxgen SBOM generation successful (or attempted).")


        for operation in self.config.get("atom_operations", []):
            op_name = operation.get("name", "unnamed_operation")
            main_cmd = operation.get("atom_main_command")
            if not main_cmd:
                logger.warning(f"Skipping operation '{op_name}' due to missing 'atom_main_command'.")
                continue

            logger.info(f"Executing atom operation: '{op_name}' ({main_cmd})")

            # Construct atom command
            # Atom CLI: atom [parsedeps|data-flow|usages|reachables] [options] [input]
            # Input is the project directory relative to /app
            atom_cmd_parts = [atom_executable, main_cmd]

            # Output files (-o and -s)
            # These paths are relative to /app inside the container
            primary_out_container = operation.get("atom_primary_output_container")
            slice_out_container = operation.get("atom_slice_output_container")
            
            if primary_out_container:
                atom_cmd_parts.extend(["-o", primary_out_container])
            if slice_out_container:
                atom_cmd_parts.extend(["-s", slice_out_container])

            # Language
            atom_cmd_parts.extend(["-l", project_lang])

            # Extra arguments
            extra_args = operation.get("extra_args", [])
            for arg in extra_args:
                atom_cmd_parts.append(str(arg).replace("{language}", project_lang)) # Substitution

            # Input directory (last argument)
            atom_cmd_parts.append(".") # Current directory within workdir

            # Execute
            exit_code, stdout, stderr = self._exec_in_container(
                atom_cmd_parts,
                workdir=f"/app/{project_source_container_path}"
            )

            if exit_code != 0:
                logger.error(f"Atom operation '{op_name}' using '{atom_executable}' failed.")
                # Continue to next operation, don't stop all processing for one failure
            else:
                logger.info(f"Atom operation '{op_name}' using '{atom_executable}' successful.")
                # Copy specified outputs
                files_to_copy = []
                if primary_out_container and operation.get("copy_primary_output", False): # Add a flag if needed
                     files_to_copy.append((f"/app/{project_source_container_path}/{primary_out_container}", 
                                           output_subdir / os.path.basename(primary_out_container)))
                if slice_out_container and operation.get("host_target_file_suffix"):
                     files_to_copy.append((f"/app/{project_source_container_path}/{slice_out_container}",
                                           output_subdir / operation["host_target_file_suffix"]))
                
                for container_src, host_dest in files_to_copy:
                    self._copy_from_container(container_src, host_dest)
        return True # Overall success of running operations (individual failures are logged)

    def _copy_from_container(self, container_path: str, host_path: pathlib.Path) -> bool:
        """Copies a file or directory from the container to the host."""
        if not self.container:
            logger.error("Container not available for copying.")
            return False
        
        logger.info(f"Copying from container '{container_path}' to host '{host_path}'...")
        host_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            # Check if path exists in container first
            exit_code, stdout, _ = self._exec_in_container(['test', '-e', container_path])
            if exit_code != 0:
                logger.warning(f"Path '{container_path}' does not exist in container. Cannot copy.")
                return False

            bits, stat = self.container.get_archive(container_path)
            
            # Write the tar archive to a temporary file
            temp_tar_path = self.base_workspace_dir / f"temp_{self.container_name}_{os.path.basename(container_path)}.tar"
            with open(temp_tar_path, 'wb') as f:
                for chunk in bits:
                    f.write(chunk)
            
            # Extract the tar archive to the destination
            # If it's a single file, it will be extracted into the parent dir with its original name.
            # If it's a dir, its contents will be extracted.
            # We want to place it *at* host_path.
            
            # If host_path is intended to be a file:
            if not host_path.suffix: # Heuristic: if no suffix, assume it's a dir
                 host_path.mkdir(parents=True, exist_ok=True) # Ensure dir exists
                 extract_to_dir = host_path
            else: # Assume it's a file
                 host_path.parent.mkdir(parents=True, exist_ok=True)
                 extract_to_dir = host_path.parent

            import tarfile
            with tarfile.open(temp_tar_path, 'r') as tar:
                # To handle cases where tar contains a single top-level dir
                members = tar.getmembers()
                if len(members) == 1 and members[0].isdir():
                    # Extract contents of this single dir into extract_to_dir
                    # We need to strip the leading directory component from member names
                    for member in members: # Re-fetch to reset internal pointer if any
                        original_name = member.name
                        member.name = os.path.relpath(member.name, members[0].name)
                        if member.name == ".": continue # Skip the directory itself
                        tar.extract(member, path=extract_to_dir)
                    # If the original host_path was a file name, and we extracted a dir
                    # we might need to move the contents.
                    # This part is tricky. For now, assume simple file copy.
                    # If container_path is a file, tar will contain that file.
                    # If host_path is file_A, it extracts to extract_to_dir/file_A_original_name
                    # This needs refinement if copying single files vs dirs.
                    # Let's assume container_path is mostly files for atom outputs.
                    
                    # Simplified: extract all, then move if necessary
                    # This logic assumes the tar contains the file directly, not in a subdirectory.
                    # If container_path is /app/data/output.json, tar will have output.json.
                    # It will be extracted to extract_to_dir/output.json
                    # If host_path was /tmp/my_output.json, we need to move extract_to_dir/output.json to /tmp/my_output.json
                    tar.extractall(path=extract_to_dir)
                    extracted_file_name = os.path.basename(container_path)
                    if (extract_to_dir / extracted_file_name).exists() and str(host_path.name) != extracted_file_name:
                        shutil.move(str(extract_to_dir / extracted_file_name), str(host_path))
                    elif not (extract_to_dir / host_path.name).exists() and (extract_to_dir / extracted_file_name).is_file():
                         # if host_path is /target/dir/desired_name.json and it extracted as /target/dir/original_name.json
                         if host_path.parent == extract_to_dir and host_path.name != extracted_file_name:
                            os.rename(extract_to_dir / extracted_file_name, host_path)


                else: # multiple files/dirs or single file not in a dir
                    tar.extractall(path=extract_to_dir)
                    # If host_path is a file, and tar extracted a file with the same name into extract_to_dir
                    # then it should be at extract_to_dir / host_path.name
                    # If that's not the case, it means the tar had a different structure.

            temp_tar_path.unlink() # Clean up temp tar
            logger.info(f"Successfully copied to {host_path}")
            return True

        except docker.errors.NotFound:
            logger.warning(f"Path '{container_path}' not found in container for copying.")
            return False
        except Exception as e:
            logger.error(f"Error copying from container: {e}")
            if 'temp_tar_path' in locals() and temp_tar_path.exists():
                temp_tar_path.unlink()
            return False

    def _compare_outputs(self) -> bool:
        """Compares JSON outputs from JAR and Native runs using custom-json-diff."""
        logger.info("Comparing JAR and Native outputs...")
        self.diff_dir.mkdir(parents=True, exist_ok=True)
        
        # Check if custom-json-diff (cjd) is available
        try:
            subprocess.run(["cjd", "--version"], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            logger.error("`custom-json-diff` (cjd) command not found or not executable.")
            logger.error("Please install it: `pip install custom-json-diff` and ensure it's in your PATH.")
            return False

        for operation in self.config.get("atom_operations", []):
            if not operation.get("is_json_diff_target", False):
                continue
            
            op_name = operation.get("name", "unnamed_operation")
            file_suffix = operation.get("host_target_file_suffix")
            if not file_suffix:
                logger.warning(f"Skipping diff for '{op_name}': missing 'host_target_file_suffix'.")
                continue

            jar_file = self.jar_output_dir / file_suffix
            native_file = self.native_output_dir / file_suffix
            diff_output_file = self.diff_dir / f"{file_suffix}.diff.json"
            # Report output can also be configured, e.g. to .html or .txt
            # diff_report_file = self.diff_dir / f"{file_suffix}.diff.html" 

            if not jar_file.exists():
                logger.warning(f"JAR output file for diff not found: {jar_file}")
                continue
            if not native_file.exists():
                logger.warning(f"Native output file for diff not found: {native_file}")
                continue

            logger.info(f"Comparing '{jar_file.name}' (JAR) vs '{native_file.name}' (Native)...")
            
            # custom-json-diff -i <older> <newer> -o <diff_json> preset-diff --type <type>
            # Assuming JAR is "older" for consistency, though order might not matter for diff content
            # The preset type might need to be configurable. Defaulting to 'bom' as per example.
            cjd_preset_type = operation.get("cjd_preset_type", "bom")
            
            cmd = [
                "cjd",
                "-i", str(jar_file), str(native_file),
                "-o", str(diff_output_file),
                "preset-diff", "--type", cjd_preset_type
            ]
            # Add other cjd options from config if needed, e.g., --allow-new-versions
            # for opt_key, opt_val in operation.get("cjd_options", {}).items():
            #    cmd.append(opt_key)
            #    if opt_val is not True: # if it's a flag like --allow-new-versions
            #        cmd.append(str(opt_val))


            if not self._run_host_command(cmd, cwd=self.project_output_path):
                logger.error(f"custom-json-diff failed for {file_suffix}")
            else:
                logger.info(f"Diff for {file_suffix} created at {diff_output_file}")
        
        return True

    def _cleanup_container(self):
        """Stops and removes the Docker container."""
        if self.container:
            logger.info(f"Cleaning up container '{self.container_name}'...")
            try:
                self.container.reload() # Get fresh status
                if self.container.status == "running":
                    self.container.stop(timeout=30)
                self.container.remove(force=True) # Force remove if stop failed or already stopped
                logger.info(f"Container '{self.container_name}' stopped and removed.")
            except docker.errors.NotFound:
                logger.info(f"Container '{self.container_name}' already removed or not found.")
            except docker.errors.APIError as e:
                logger.error(f"Error cleaning up container '{self.container_name}': {e}")
            finally:
                self.container = None
        else:
            logger.info(f"No active container named '{self.container_name}' to cleanup for this processor instance.")


    def process(self) -> bool:
        """Main processing logic for the project."""
        logger.info(f"--- Starting processing for project: {self.project_lang}/{self.project_name} ---")
        
        # 0. Create output dirs
        self.project_output_path.mkdir(parents=True, exist_ok=True)

        # 1. Clone repository
        if not self._clone_repo():
            logger.error("Failed to clone repository. Aborting project.")
            return False

        # 2. Start Docker container
        if not self._start_container():
            logger.error("Failed to start Docker container. Aborting project.")
            self._cleanup_container() # Attempt cleanup even if start failed partially
            return False

        try:
            # 3. Install atom tools in container (once per container lifetime)
            if not self._install_atom_tools_in_container():
                logger.error("Failed to install atom tools in container. Aborting project.")
                return False # No 'finally' here, _cleanup_container will be called by main loop

            # 4. Run project-specific install/build commands
            if not self._run_project_install_build_in_container():
                logger.error("Failed to run project install/build commands in container. Aborting project.")
                return False

            # 5. Run atom operations (JAR version)
            logger.info("=== Running Atom (NPM/JAR Version) ===")
            if not self._run_atom_operations("atom", self.jar_output_dir):
                logger.error("Atom (JAR) operations encountered errors.")
                # Decide if this is fatal or if we should still try native
                # For now, continue to try native if JAR fails

            # 6. Run atom operations (Native version)
            logger.info(f"=== Running Atom (Native Version - {ATOM_NATIVE_EXECUTABLE_NAME}) ===")
            if not self._run_atom_operations(ATOM_NATIVE_EXECUTABLE_NAME, self.native_output_dir):
                logger.error(f"Atom ({ATOM_NATIVE_EXECUTABLE_NAME}) operations encountered errors.")
                # Continue to comparison if some files were generated

            # 7. Compare outputs
            if not self._compare_outputs():
                logger.warning("Output comparison step failed or had issues.")
                # Not necessarily a fatal error for the whole process

            logger.info(f"--- Finished processing for project: {self.project_lang}/{self.project_name} ---")
            return True

        except Exception as e:
            logger.error(f"An unexpected error occurred during processing of {self.project_name}: {e}", exc_info=True)
            return False
        finally:
            # 8. Cleanup container
            self._cleanup_container()


