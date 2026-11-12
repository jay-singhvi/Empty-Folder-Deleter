import os
import fnmatch


def generate_file_structure(
    path=None,
    output_file="Project-File-Structure.txt",
    exclude_folder_list=None,
    exclude_filetype_list=None,
    include_folder_list=None,
    include_filetype_list=None,
):
    """
    Generate a text file listing all files and directories in a specified path.

    Logic:
    - If include lists are specified, only those items are initially included
    - Then exclude lists are applied to filter out from the included items
    - If no include lists are specified, all items are included by default, then exclusions are applied

    Parameters:
    -----------
    path : str, optional
        The root directory to analyze. If None, uses current working directory.
    output_file : str
        The name of the output file (default: "Project-File-Structure.txt")
    exclude_folder_list : list
        List of folder paths to exclude
    exclude_filetype_list : list
        List of file extensions to exclude (without dots, e.g. ["py", "ipynb"])
    include_folder_list : list
        List of folder paths to specifically include
    include_filetype_list : list
        List of file extensions to specifically include (without dots)
    """
    # Use current working directory if path is not provided
    if path is None:
        path = os.getcwd()

    # Initialize empty lists if parameters are None
    exclude_folder_list = exclude_folder_list or []
    exclude_filetype_list = exclude_filetype_list or []
    include_folder_list = include_folder_list or []
    include_filetype_list = include_filetype_list or []

    # Convert all file extensions to lowercase with dots for consistent comparison
    exclude_filetype_list = [
        "." + ext.lower().lstrip(".") for ext in exclude_filetype_list
    ]
    include_filetype_list = [
        "." + ext.lower().lstrip(".") for ext in include_filetype_list
    ]

    # Normalize paths for consistency
    path = os.path.normpath(path)
    exclude_folder_list = [os.path.normpath(folder) for folder in exclude_folder_list]
    include_folder_list = [os.path.normpath(folder) for folder in include_folder_list]

    # Flag to determine if we're using inclusion filtering for folders and file types
    use_folder_inclusion = bool(include_folder_list)
    use_filetype_inclusion = bool(include_filetype_list)

    # List to store all matching file paths
    file_paths = []

    # Walk through directory
    for root, dirs, files in os.walk(path):
        # Skip .venv directories like in the PowerShell script
        if ".venv" in root:
            continue

        # Check folder inclusion/exclusion
        relative_root = os.path.relpath(root, path)

        # First apply inclusion logic if specified
        if use_folder_inclusion:
            # Check if this folder or any parent folder is in the include list
            is_included = False
            current_path = relative_root

            # Check the folder itself and all its parent folders
            while current_path and not is_included:
                is_included = any(
                    fnmatch.fnmatch(current_path, include_pattern)
                    or fnmatch.fnmatch(
                        os.path.join(path, current_path), include_pattern
                    )
                    for include_pattern in include_folder_list
                )
                # Move up to parent directory
                current_path = os.path.dirname(current_path)

            # Skip if folder is not included
            if not is_included:
                continue

        # Then apply exclusion logic
        # Check if this folder or any parent folder is in the exclude list
        is_excluded = False
        current_path = relative_root

        # Check the folder itself and all its parent folders
        while current_path and not is_excluded:
            is_excluded = any(
                fnmatch.fnmatch(current_path, exclude_pattern)
                or fnmatch.fnmatch(os.path.join(path, current_path), exclude_pattern)
                for exclude_pattern in exclude_folder_list
            )
            # Move up to parent directory
            current_path = os.path.dirname(current_path)

        # Skip if folder is excluded
        if is_excluded:
            continue

        # Process files in this directory
        for file in files:
            full_path = os.path.join(root, file)
            _, ext = os.path.splitext(file)
            ext = ext.lower()

            # First apply inclusion logic for file types if specified
            if use_filetype_inclusion and ext not in include_filetype_list:
                continue

            # Then apply exclusion logic for file types
            if ext in exclude_filetype_list:
                continue

            # Add file to our list
            file_paths.append(full_path)

    # Write results to output file
    with open(output_file, "w") as f:
        for file_path in file_paths:
            f.write(f"{file_path}\n")

    print(f"File structure written to {output_file}")
    return file_paths


# Example usage:
generate_file_structure(
    path=".",
    exclude_folder_list=[],
    exclude_filetype_list=["pyc"],
    include_folder_list=[
        "Common_Utils/BalancingAlgorithms",
        "Models/classification/Step 1 - TL_Model",
    ],
    include_filetype_list=[],
)
