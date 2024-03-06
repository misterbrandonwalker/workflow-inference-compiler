import copy
import logging
import json
import glob
import os
from pathlib import Path
import re
import sys
import tempfile
import traceback
from typing import Any, Dict, Union

import cwltool.load_tool
import yaml
import podman
import docker

from . import input_output as io
from .python_cwl_adapter import import_python_file
from .wic_types import Cwl, NodeData, RoseTree, StepId, Tool, Tools


# Filter out the "... previously defined" id uniqueness validation warnings
# from line 1162 of ref_resolver.py in the schema_salad library.
# TODO: Figure out if there is a problem with our autogenerated CWL.
class NoPreviouslyDefinedFilter(logging.Filter):
    # pylint:disable=too-few-public-methods
    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().endswith('previously defined')


class NoResolvedFilter(logging.Filter):
    # pylint:disable=too-few-public-methods
    def filter(self, record: logging.LogRecord) -> bool:
        m = re.match(r"Resolved '.*' to '.*'", record.getMessage())
        return not bool(m)  # (True if m else False)


class NoPartialFailureNullWarning(logging.Filter):
    # pylint:disable=too-few-public-methods
    def filter(self, record: logging.LogRecord) -> bool:
        err_str = "Source is from conditional step and may produce `null`"
        # this will indiscriminately filter this error string
        return not err_str in record.getMessage()


def logging_filters(allow_pf: bool = False) -> None:
    logger_salad = logging.getLogger("salad")
    logger_salad.addFilter(NoPreviouslyDefinedFilter())
    logger_cwltool = logging.getLogger("cwltool")
    logger_cwltool.addFilter(NoResolvedFilter())
    if allow_pf:
        logger_cwltool.addFilter(NoPartialFailureNullWarning())


logger_wicad = logging.getLogger("wicautodiscovery")


def validate_cwl(cwl_path_str: str, skip_schemas: bool) -> None:
    """This is the body of cwltool.load_tool.load_tool but exposes skip_schemas for performance.
    Skipping significantly improves initial validation performance, but this is not always desired.
    See https://github.com/common-workflow-language/cwltool/issues/623

    Args:
        cwl_path_str (str): The path to the CWL file.
        skip_schemas (bool): Skips processing $schemas tags.
    """
    # NOTE: This uses NoResolvedFilter to suppress the info messages to stdout.
    loading_context, workflowobj, uri = cwltool.load_tool.fetch_document(cwl_path_str)
    # NOTE: There has been a breaking change in the API for skip_schemas.
    # TODO: re-enable skip_schemas while satisfying mypy
    # loading_context.skip_schemas = skip_schemas
    loading_context, uri = cwltool.load_tool.resolve_and_validate_document(
        loading_context, workflowobj, uri, preprocess_only=False  # , skip_schemas=skip_schemas
    )
    # NOTE: Although resolve_and_validate_document does some validation,
    # some additional validation is done in make_tool, i.e.
    # resolve_and_validate_document does not in fact throw an exception for
    # some invalid CWL files, but make_tool does!
    process_ = cwltool.load_tool.make_tool(uri, loading_context)
    # return process_ # ignore process_ for now


def get_tools_cwl(homedir: str, validate_plugins: bool = False,
                  skip_schemas: bool = False, quiet: bool = False) -> Tools:
    """Uses glob() to find all of the CWL CommandLineTool definition files within any subdirectory of cwl_dir

    Args:
        homedir (str): The users home directory
        cwl_dirs_file (Path): The subdirectories in which to search for CWL CommandLineTools
        validate_plugins (bool, optional): Performs validation on all CWL CommandLiineTools. Defaults to False.
        skip_schemas (bool, optional): Skips processing $schemas tags. Defaults to False.
        quiet (bool, optional): Determines whether it captures stdout or stderr. Defaults to False.

    Returns:
        Tools: The CWL CommandLineTool definitions found using glob()
    """
    io.copy_config_files(homedir)
    # Load ALL of the tools.
    tools_cwl: Tools = {}
    cwl_dirs_file = Path(homedir) / 'wic' / 'cwl_dirs.txt'
    cwl_dirs = io.read_lines_pairs(cwl_dirs_file)
    for plugin_ns, cwl_dir in cwl_dirs:
        # "PurePath.relative_to() requires self to be the subpath of the argument, but os.path.relpath() does not."
        # See https://docs.python.org/3/library/pathlib.html#id4 and
        # See https://stackoverflow.com/questions/67452690/pathlib-path-relative-to-vs-os-path-relpath
        pattern_cwl = str(Path(cwl_dir) / '**/*.cwl')
        # print(pattern_cwl)
        cwl_paths = glob.glob(pattern_cwl, recursive=True)
        Path('autogenerated/schemas/tools/').mkdir(parents=True, exist_ok=True)
        if len(cwl_paths) == 0:
            logger_wicad.warning(f'Warning! No cwl files found in {cwl_dir}.\nCheck {cwl_dirs_file.absolute()}')
            logger_wicad.warning('This almost certainly means you are not in the correct directory.')

        for cwl_path_str in cwl_paths:
            if 'biobb_md' in cwl_path_str:
                continue  # biobb_md is deprecated (in favor of biobb_gromacs)
            # print(cwl_path)
            with open(cwl_path_str, mode='r', encoding='utf-8') as f:
                tool: Cwl = yaml.safe_load(f.read())
            stem = Path(cwl_path_str).stem
            # print(stem)

            if validate_plugins:
                validate_cwl(cwl_path_str, skip_schemas)
            if quiet:
                # Capture stdout and stderr
                if not 'stdout' in tool:
                    tool.update({'stdout': f'{stem}.out'})
                if not 'stderr' in tool:
                    tool.update({'stderr': f'{stem}.err'})
            cwl_path_abs = os.path.abspath(cwl_path_str)
            tools_cwl[StepId(stem, plugin_ns)] = Tool(cwl_path_abs, tool)
            # print(tool)
            # utils_graphs.make_tool_dag(stem, (cwl_path_str, tool))
    return tools_cwl


def cwl_update_outputs_optional(cwl: Cwl) -> Cwl:
    """Updates outputs as optional (if any)

    Args:
        cwl (Cwl): A CWL CommandLineTool

    Returns:
        Cwl: A CWL CommandLineTool with outputs optional (if any)
    """
    cwl_mod = copy.deepcopy(cwl)
    # Update success codes to allow simple failures
    cwl_mod['successCodes'] = [0, 1]
    # Update outputs optional
    for out_key, out_val_dict in cwl_mod['outputs'].items():
        if isinstance(out_val_dict['type'], str) and out_val_dict['type'][-1] != '?':
            out_val_dict['type'] += '?'
    return cwl_mod


Client = Union[docker.DockerClient, podman.PodmanClient]  # type: ignore


def remove_entrypoints(client: Client, build: Any) -> None:
    """remove/overwrite the entrypoints from all images

    https://cwl.discourse.group/t/override-docker-entrypoint-in-command-line-tool/695/2
    "Here is what the CWL standards have to say about software container entrypoints"
    "I recommend changing your docker container to not use ENTRYPOINT."

    This script will remove / overwrite the entrypoints in ALL of the docker images on your local machine.
    (It will append -noentrypoint to the version tag.)

    The reason is that we want to simultaneously allow
    1. release environments, where users will simply run code in a docker image and
    2. dev/test environments, where developers can run the latest code on the host machine and/or in the CI.

    With entrypoints, there is no easy way to switch between 1 and 2; developers will
    have to manually prepend the entrypoint string to the baseCommand, and/or possibly
    modify paths to be w.r.t. their host machine instead of w.r.t. the image. (/opt/.../main.py)

    Without entrypoints, to switch between 1 and 2, simply comment out DockerRequirement ... that's it!
    The point is that we want a uniform API so that we can programmatically switch between 1 and 2 in the CI.
    We want to run the integration tests first, and only push releases to dockerhub when the tests pass (NOT vice versa!).
    """
    # Get a list of all Docker images
    images = client.images.list()

    # Iterate over each container
    for image in images:
        if len(image.attrs['RepoTags']) != 0 and len(image.tags) != 0:
            for tag in image.tags:
                if tag.endswith('-noentrypoint'):
                    continue

                # Define the content of the Dockerfile
                dockerfile_content = f'''
                FROM {tag}
                ENTRYPOINT []
                '''
                with tempfile.TemporaryDirectory() as tempdir:
                    with open(tempdir + '/Dockerfile_tmp', mode='w', encoding='utf-8') as f:
                        f.write(dockerfile_content)

                    # Build the new Docker image from the Dockerfile
                    new_image, build_logs = build.build(
                        path=tempdir,
                        dockerfile="Dockerfile_tmp",
                        tag=f"{tag}-noentrypoint"
                    )


def remove_entrypoints_docker() -> None:
    """remove/overwrite the entrypoints from all images (using docker)
    """
    client = docker.from_env()  # type: ignore
    remove_entrypoints(client, client.images)


def remove_entrypoints_podman() -> None:
    """remove/overwrite the entrypoints from all images (using podman)
    """
    # See https://github.com/containers/podman-py?tab=readme-ov-file#example-usage
    uri = "unix:///run/user/1000/podman/podman.sock"

    with podman.PodmanClient(base_url=uri) as client:
        remove_entrypoints(client, podman.domain.images_build.BuildMixin())


def cwl_update_outputs_optional_rosetree(rose_tree: RoseTree) -> RoseTree:
    """Updates outputs optional for every CWL CommandLineTool

    Args:
        rose_tree (RoseTree): The RoseTree returned from compile_workflow(...).rose_tree

    Returns:
        RoseTree: rose_tree with output optional updates to every CWL CommandLineTool
    """
    n_d: NodeData = rose_tree.data
    if n_d.compiled_cwl['class'] == 'CommandLineTool':
        outputs_optional_cwl = cwl_update_outputs_optional(n_d.compiled_cwl)
    else:
        outputs_optional_cwl = n_d.compiled_cwl

    sub_trees_path = [cwl_update_outputs_optional_rosetree(sub_rose_tree) for
                      sub_rose_tree in rose_tree.sub_trees]
    node_data_path = NodeData(n_d.namespaces, n_d.name, n_d.yml, outputs_optional_cwl, n_d.tool,
                              n_d.workflow_inputs_file, n_d.explicit_edge_defs, n_d.explicit_edge_calls,
                              n_d.graph, n_d.inputs_workflow, n_d.step_name_1)
    return RoseTree(node_data_path, sub_trees_path)


def dockerPull_append_noentrypoint(cwl: Cwl) -> Cwl:
    """Appends -noentrypoint to the dockerPull version tag (if any)

    Args:
        cwl (Cwl): A CWL CommandLineTool

    Returns:
        Cwl: A CWL CommandLineTool, with -noentrypoint appended to the dockerPull version tag (if any)
    """
    docker_image: str = cwl.get('requirements', {}).get('DockerRequirement', {}).get('dockerPull', '')
    if docker_image:
        print('docker_image', docker_image)
    if ':' in docker_image:
        repo, tag = docker_image.split(':')
    else:
        repo, tag = docker_image, 'latest'
    if repo and tag and not tag.endswith('-noentrypoint'):
        print('repo, tag', repo, tag)
        image_noentrypoint = repo + ':' + tag + '-noentrypoint'
        cwl_noentrypoint = copy.deepcopy(cwl)
        cwl_noentrypoint['requirements']['DockerRequirement']['dockerPull'] = image_noentrypoint
        return cwl_noentrypoint
    else:
        return cwl


def dockerPull_append_noentrypoint_tools(tools: Tools) -> Tools:
    """Appends -noentrypoint to the dockerPull version tag for every tool in tools.

    Args:
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl()

    Returns:
        Tools: tools with -noentrypoint appended to all of the dockerPull version tags.
    """
    return {stepid: Tool(tool.run_path, dockerPull_append_noentrypoint(tool.cwl))
            for stepid, tool in tools.items()}


def dockerPull_append_noentrypoint_rosetree(rose_tree: RoseTree) -> RoseTree:
    """Appends -noentrypoint to the dockerPull version tag for every CWL CommandLineTool

    Args:
        rose_tree (RoseTree): The RoseTree returned from compile_workflow(...).rose_tree

    Returns:
        RoseTree: rose_tree with -noentrypoint appended to the dockerPull version tag for every CWL CommandLineTool
    """
    n_d: NodeData = rose_tree.data
    # NOTE: Since only class: CommandLineTool should have dockerPull tags,
    # this should be the identity function on class: Workflow.
    compiled_cwl_noent = dockerPull_append_noentrypoint(n_d.compiled_cwl)

    sub_trees_noent = [dockerPull_append_noentrypoint_rosetree(sub_rose_tree) for sub_rose_tree in rose_tree.sub_trees]
    node_data_noent = NodeData(n_d.namespaces, n_d.name, n_d.yml, compiled_cwl_noent, n_d.tool, n_d.workflow_inputs_file,
                               n_d.explicit_edge_defs, n_d.explicit_edge_calls, n_d.graph, n_d.inputs_workflow,
                               n_d.step_name_1)
    return RoseTree(node_data_noent, sub_trees_noent)


def cwl_prepend_dockerFile_include_path(cwl: Cwl, cwl_path: str) -> Cwl:
    """Prepends (original) cwl_path to the dockerFile $include path

    Args:
        cwl (Cwl): A CWL CommandLineTool
        cwl_path : Specifies the absolute path of .cwl file
    Returns:
        Cwl: A CWL CommandLineTool, with correct filepath prepended
    """
    cwl_mod = copy.deepcopy(cwl)
    inc_path: str = cwl.get('hints', {}).get('DockerRequirement', {}).get('dockerFile', {}).get('$include', '')
    if inc_path:
        # cwl_path is an absolute path including (original) filename .cwl
        # we just need to prepend the dir of cwl_path to the inc_path
        cwl_dir: str = str(Path(cwl_path).parent)
        cwl_mod['hints']['DockerRequirement']['dockerFile']['$include'] = cwl_dir + '/' + inc_path
    return cwl_mod


def cwl_prepend_dockerFile_include_path_rosetree(rose_tree: RoseTree) -> RoseTree:
    """Prepends (original) cwl_path to the dockerFile $include path to every CWL CommandLineTool

    Args:
        rose_tree (RoseTree): The RoseTree returned from compile_workflow(...).rose_tree

    Returns:
        RoseTree: rose_tree with (original) cwl_path to the dockerFile to the dockerFile $include path
        to every CWL CommandLineTool
    """
    n_d: NodeData = rose_tree.data
    # NOTE: Since only class: CommandLineTool should have dockerPull tags,
    # this should be the identity function on class: Workflow.
    prepended_cwl = cwl_prepend_dockerFile_include_path(n_d.compiled_cwl, n_d.tool.run_path)

    sub_trees_path = [cwl_prepend_dockerFile_include_path_rosetree(sub_rose_tree) for
                      sub_rose_tree in rose_tree.sub_trees]
    node_data_path = NodeData(n_d.namespaces, n_d.name, n_d.yml, prepended_cwl, n_d.tool, n_d.workflow_inputs_file,
                              n_d.explicit_edge_defs, n_d.explicit_edge_calls, n_d.graph, n_d.inputs_workflow,
                              n_d.step_name_1)
    return RoseTree(node_data_path, sub_trees_path)


def get_workflow_paths(homedir: str, extension: str) -> Dict[str, Dict[str, Path]]:
    """Uses glob() to recursively find all of the yml workflow definition files
    within any subdirectory of each yml_dir in yml_dirs_file.
    NOTE: This function assumes all yml files found are workflow definition files,
    so do not mix regular yml files and workflow files in the same root directory.
    Moreover, each yml_dir should be disjoint; do not use both '.' and './subdir'!

    Args:
        homedir (str): The users home directory
        extension (str): The filename extension (either 'yml' or 'py')

    Returns:
        Dict[str, Dict[str, Path]]: A dict containing the filepath stem and filepath of each yml file
    """
    io.copy_config_files(homedir)
    yml_dirs_file = Path(homedir) / 'wic' / 'yml_dirs.txt'
    yml_dirs = io.read_lines_pairs(yml_dirs_file)
    # Glob all of the yml files too, so we don't have to deal with relative paths.
    yml_paths_all: Dict[str, Dict[str, Path]] = {}
    for yml_namespace, yml_dir in yml_dirs:
        # "PurePath.relative_to() requires self to be the subpath of the argument, but os.path.relpath() does not."
        # See https://docs.python.org/3/library/pathlib.html#id4 and
        # See https://stackoverflow.com/questions/67452690/pathlib-path-relative-to-vs-os-path-relpath
        pattern_yml = str(Path(yml_dir) / f'**/*.{extension}')
        yml_paths_sorted = sorted(glob.glob(pattern_yml, recursive=True), key=len, reverse=True)
        Path('autogenerated/schemas/workflows/').mkdir(parents=True, exist_ok=True)
        if len(yml_paths_sorted) == 0:
            print(f'Warning! No {extension} files found in {yml_dir}.\nCheck {yml_dirs_file.absolute()}')
            print('This almost certainly means you are not in the correct directory.')
        yml_paths = {}
        for yml_path_str in yml_paths_sorted:
            # Exclude our autogenerated inputs files
            if '_inputs' not in yml_path_str:
                yml_path = Path(yml_path_str)
                yml_path_abs = os.path.abspath(yml_path_str)
                yml_paths[yml_path.stem] = Path(yml_path_abs)
        # Check for existing entry (so we can split a single
        # namespace across multiple lines in yml_dirs.txt)
        ns_dict = yml_paths_all.get(yml_namespace, {})
        yml_paths_all[yml_namespace] = {**ns_dict, **yml_paths}

    return yml_paths_all


def get_yml_paths(homedir: str) -> Dict[str, Dict[str, Path]]:
    return get_workflow_paths(homedir, 'yml')


def get_py_paths(homedir: str) -> Dict[str, Dict[str, Path]]:
    return get_workflow_paths(homedir, 'py')


def blindly_execute_python_workflows() -> None:
    """This function imports (read: blindly executes) all python files in yml_dirs.txt
       The python files are assumed to have a top-level workflow() function
       which returns a wic.api.pythonapi.Workflow object.
       The python files should NOT call the .run() method!
       (from any code path that is automatically executed on import)
    """
    # I hope u like Remote Code Execution vulnerabilities!
    # See https://en.wikipedia.org/wiki/Arithmetical_hierarchy
    from wic.api import pythonapi  # pylint: disable=C0415:import-outside-toplevel
    pythonapi.global_config = get_tools_cwl(str(Path().home()))  # Use path fallback in the CI
    paths = get_py_paths(str(Path().home()))
    paths_tuples = [(path_str, path)
                    for namespace, paths_dict in paths.items()
                    for path_str, path in paths_dict.items()]
    any_import_errors = False
    for path_stem, path in paths_tuples:
        if 'mm-workflows' in str(path) or 'docs/tutorials/' in str(path):
            # Exclude paths that only contain 'regular' python files.
            continue
        # NOTE: Use anything (unique?) for the python_module_name.
        try:
            module = import_python_file(path_stem, path)
            # Let's require all python API files to define a function, say
            # def workflow() -> Workflow
            # so we can programmatically call it here:
            retval: pythonapi.Workflow = module.workflow()  # no arguments
            # which allows us to programmatically call Workflow methods:
            compiler_info = retval.compile()  # hopefully retval is actually a Workflow object!
            # But since this is python (i.e. not Haskell) that in no way eliminates
            # the above security considerations.

            # This lets us use path.parent to write a *.yml file in the
            # auto-discovery path, and thus re-use the existing wic CI
            retval.write_ast_to_disk(path.parent)

            # Programmatically blacklist subworkflows from running in config_ci.json
            # (Again, because subworkflows are missing inputs and cannot run.)
            config_ci = path.parent / 'config_ci.json'
            json_contents = {}
            if config_ci.exists():
                with open(config_ci, mode='r', encoding='utf-8') as r:
                    json_contents = json.load(r)
            run_blacklist: list[str] = json_contents.get('run_blacklist', [])
            # Use [1:] for proper subworkflows only
            subworkflows: list[pythonapi.Workflow] = retval.flatten_subworkflows()[1:]
            run_blacklist += [wf.process_name for wf in subworkflows]
            json_contents['run_blacklist'] = run_blacklist
            with open(config_ci, mode='w', encoding='utf-8') as f:
                json.dump(json_contents, f)

        except Exception as e:
            any_import_errors = True
            if sys.version_info >= (3, 10):
                traceback.print_exception(type(e), value=e, tb=None)
            else:
                traceback.print_exception(etype=type(e), value=e, tb=None)
    if any_import_errors:
        sys.exit(1)  # Make sure the CI fails
