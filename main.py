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
from projectProcess import ProjectProcessor

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(module)s - %(message)s',
                    handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)

# Default directories
DEFAULT_INPUT_DIR = "atom_inputs"
DEFAULT_OUTPUT_DIR = "atom_outputs"
DEFAULT_WORKSPACE_DIR = "atom_workspace" # For cloning repos and intermediate files


def main():
    parser = argparse.ArgumentParser(description="Automate atom tool analysis using Docker.")
    parser.add_argument("--input-dir", type=pathlib.Path, default=DEFAULT_INPUT_DIR,
                        help=f"Base directory containing project configurations (default: {DEFAULT_INPUT_DIR})")
    parser.add_argument("--output-dir", type=pathlib.Path, default=DEFAULT_OUTPUT_DIR,
                        help=f"Base directory to store output files (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--workspace-dir", type=pathlib.Path, default=DEFAULT_WORKSPACE_DIR,
                        help=f"Directory for cloning repos and temporary files (default: {DEFAULT_WORKSPACE_DIR})")
    parser.add_argument("--project", type=str, default=None,
                        help="Specify a single project to process (format: Language/ProjectName). Processes all if not set.")
    parser.add_argument("--skip-cloning", action="store_true", help="Skip cloning repositories (assumes they exist).")
    parser.add_argument("--skip-docker-tools-install", action="store_true", help="Skip installing tools in Docker (assumes already set up).")
    parser.add_argument("--skip-jar", action="store_true", help="Skip running atom JAR version.")
    parser.add_argument("--skip-native", action="store_true", help="Skip running atom Native version.")
    parser.add_argument("--skip-compare", action="store_true", help="Skip comparison using custom-json-diff.")
    parser.add_argument("--keep-containers", action="store_true", help="Do not remove containers after processing (for debugging).")


    args = parser.parse_args()

    # Ensure directories exist
    args.input_dir.mkdir(parents=True, exist_ok=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.workspace_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Input directory: {args.input_dir.resolve()}")
    logger.info(f"Output directory: {args.output_dir.resolve()}")
    logger.info(f"Workspace directory: {args.workspace_dir.resolve()}")

    try:
        docker_client = docker.from_env()
        docker_client.ping() # Test connection
        logger.info("Docker client initialized and connected.")
    except Exception as e:
        logger.error(f"Failed to initialize Docker client: {e}")
        logger.error("Please ensure Docker is running and accessible.")
        return

    project_config_files = []
    if args.project:
        lang, proj_name = args.project.split('/', 1)
        path = args.input_dir / lang / proj_name / "project_config.json"
        if path.is_file():
            project_config_files.append(path)
        else:
            logger.error(f"Specified project config not found: {path}")
            return
    else:
        for lang_dir in args.input_dir.iterdir():
            if lang_dir.is_dir():
                for project_dir in lang_dir.iterdir():
                    if project_dir.is_dir():
                        config_file = project_dir / "project_config.json"
                        if config_file.is_file():
                            project_config_files.append(config_file)

    if not project_config_files:
        logger.warning(f"No project configuration files found in {args.input_dir}.")
        logger.warning("Expected structure: INPUT_DIR/Language/ProjectName/project_config.json")
        return

    logger.info(f"Found {len(project_config_files)} project(s) to process.")

    overall_success = True
    for config_path in project_config_files:
        processor = None 
        try:
            processor = ProjectProcessor(config_path, args.input_dir, args.workspace_dir, args.output_dir, docker_client)

            if args.skip_cloning: processor._clone_repo = lambda: True
            if args.skip_docker_tools_install: processor.tools_installed_in_container = True 

            if not processor.process():
                logger.error(f"Processing failed for {processor.project_lang}/{processor.project_name}")
                overall_success = False
            
        except ValueError as ve: 
            logger.error(f"Skipping project due to config error: {config_path} - {ve}")
            overall_success = False
        except Exception as e:
            logger.error(f"Critical error setting up processor for {config_path}: {e}", exc_info=True)
            overall_success = False
        finally:
            if processor and args.keep_containers and processor.container:
                logger.info(f"Keeping container {processor.container_name} as per --keep-containers flag.")
            elif processor and not args.keep_containers:
                 processor._cleanup_container()


    if overall_success:
        logger.info("All projects processed successfully.")
    else:
        logger.warning("One or more projects failed during processing.")

if __name__ == "__main__":
    main()
