import os


def resolve_path_within_directory(
    base_dir: str,
    unsafe_path: str,
    *,
    require_file: bool = True,
) -> str:
    # The path passed in by the user may be a file name, relative path, absolute path, or may contain `../`.
    # Here, it is uniformly parsed into the real path, and commonpath is used to determine whether it is still within the allowed directory.
    # This is more reliable than simply determining the string prefix, and can cover symbolic links, repeated delimiters, and relative paths.
    # It is suitable for whitelist directories such as upload directories, material directories, and work product directories.
    if not unsafe_path:
        raise ValueError("empty path is not allowed")

    base_dir_real = os.path.realpath(base_dir)
    candidate_path = unsafe_path
    if not os.path.isabs(candidate_path):
        candidate_path = os.path.join(base_dir_real, candidate_path)

    resolved_path = os.path.realpath(candidate_path)
    try:
        common_path = os.path.commonpath([base_dir_real, resolved_path])
    except ValueError as exc:
        # Different drive letters under Windows will trigger ValueError, and such paths must not belong to allowed directories.
        raise ValueError("path is outside the allowed directory") from exc

    if common_path != base_dir_real:
        raise ValueError("path is outside the allowed directory")

    if require_file and not os.path.isfile(resolved_path):
        raise ValueError("file does not exist")

    return resolved_path
