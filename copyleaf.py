import os
import shutil

def copy_to_leaf_folders(source_file_path, root_folder_path):
    """
    Copies a source file to all leaf subdirectories within a root folder.

    A leaf subdirectory is a directory that does not contain any other directories.

    Args:
        source_file_path (str): The path to the file to be copied.
        root_folder_path (str): The path to the root folder to search within.
    """
    # --- Input Validation ---
    if not os.path.isfile(source_file_path):
        print(f"Error: Source file '{source_file_path}' not found or is not a file.")
        return

    if not os.path.isdir(root_folder_path):
        print(f"Error: Root folder '{root_folder_path}' not found or is not a directory.")
        return

    copied_to_folders = [] # To store paths of folders where the file was copied

    # --- Recursive Traversal ---
    # os.walk generates the directory names, subdirectory names, and file names
    # for each directory in the tree, in a top-down manner.
    for dirpath, dirnames, filenames in os.walk(root_folder_path):
        # Check if the current directory (dirpath) has no subdirectories
        if not dirnames:  # This means it's a leaf folder
            try:
                # Construct the full destination path for the file
                destination_file_path = os.path.join(dirpath, os.path.basename(source_file_path))

                # Copy the file
                # shutil.copy2 attempts to preserve all file metadata
                shutil.copy2(source_file_path, destination_file_path)
                copied_to_folders.append(dirpath)
            except Exception as e:
                print(f"Error copying to '{dirpath}': {e}")

    # --- Output ---
    if copied_to_folders:
        print(f"\nFile '{os.path.basename(source_file_path)}' copied to the following leaf folders:")
        for folder in copied_to_folders:
            print(folder)
    else:
        print(f"\nNo leaf folders found in '{root_folder_path}', or an error occurred during copying.")

if __name__ == "__main__":
    print("--- Recursive File Copier ---")
    print("This script copies a file into all subdirectories of a chosen folder")
    print("that do not themselves contain any further subdirectories (leaf folders).\n")

    # Get the path to the file to be copied
    source_file = input("Enter the full path of the file to copy: ").strip()

    # Get the path to the root folder
    target_root_folder = input("Enter the full path of the root folder to process: ").strip()

    # Call the function to perform the copy operation
    copy_to_leaf_folders(source_file, target_root_folder)

    print("\n--- Operation Complete ---")
