""" This file manages the overall configuration of the system for running
simulation tasks. """

from __future__ import annotations

import re
from datetime import timedelta
from time import strftime, gmtime
import pprint
import logging
import yaml
import os
import sys
from fabric.operations import _stdoutString # type: ignore
from fabric.api import prefix, settings, local, run # type: ignore
from fabric.contrib.project import rsync_project # type: ignore
from os.path import join as pjoin
from os.path import basename, expanduser
from pathlib import Path
from uuid import uuid1
from tempfile import TemporaryDirectory
import hashlib
import json

from awstools.awstools import aws_resource_names
from awstools.afitools import get_firesim_deploy_quintuplet_for_agfi, firesim_description_to_tags
from runtools.firesim_topology_with_passes import FireSimTopologyWithPasses
from runtools.run_farm_deploy_managers import VitisInstanceDeployManager
from runtools.workload import WorkloadConfig
from runtools.run_farm import RunFarm
from runtools.simulation_data_classes import TipTracingConfig, TracerVConfig, AutoCounterConfig, HostDebugConfig, SynthPrintConfig, PartitionConfig, TipTracingConfig
from util.inheritors import inheritors
from util.deepmerge import deep_merge
from util.streamlogger import InfoStreamLogger
from buildtools.bitbuilder import get_deploy_dir
from util.io import downloadURI
from util.privatedownload import downloadURIPrivate

from typing import Optional, Dict, Any, List, Sequence, Tuple, TYPE_CHECKING
import argparse # this is not within a if TYPE_CHECKING: scope so the `register_task` in FireSim can evaluate it's annotation
if TYPE_CHECKING:
    from runtools.utils import MacAddress

LOCAL_DRIVERS_BASE = "../sim/output"
LOCAL_DRIVERS_GENERATED_SRC = "../sim/generated-src"
CUSTOM_RUNTIMECONFS_BASE = "../sim/custom-runtime-configs"

rootLogger = logging.getLogger()

# from  https://github.com/pandas-dev/pandas/blob/96b036cbcf7db5d3ba875aac28c4f6a678214bfb/pandas/io/common.py#L73
_RFC_3986_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9+\-+.]*://")

class URIContainer:
    """ A class which contains the details for downloading a single URI. """

    """a property name on RuntimeHWConfig"""
    hwcfg_prop: str
    """ the final filename inside sim_slot_x, this is a filename, not a path"""
    destination_name: str

    def __init__(self, hwcfg_prop: str, destination_name: str):
        self.hwcfg_prop = hwcfg_prop
        self.destination_name = destination_name

    # this filename will be used when pre-downloading
    @classmethod
    def hashed_name(cls, uri: str) -> str:
        m = hashlib.sha256()
        m.update(bytes(uri, 'utf-8'))
        return m.hexdigest()

    def _resolve_vanilla_path(self, hwcfg: RuntimeHWConfig) -> Optional[str]:
        """ Allows fallback to a vanilla path. Relative paths are resolved relative to firesim/deploy/.
        This will convert a vanilla path to a URI, or return None."""
        uri: Optional[str] = getattr(hwcfg, self.hwcfg_prop)

        # do nothing if there isn't a URI
        if uri is None:
            return None

        # if already a URI, exit early returning unmodified string
        is_uri = re.match(_RFC_3986_PATTERN, uri)
        if is_uri:
            return uri

        # expanduser() is required to get ~ home directory expansion working
        # relative paths are relative to firesim/deploy
        expanded = Path(expanduser(uri))

        try:
            # strict=True will throw if the file doesn't exist
            resolved = expanded.resolve(strict=True)
        except FileNotFoundError as e:
            raise Exception(f"{self.hwcfg_prop} file fallback at path '{uri}' or '{expanded}' was not found")

        return f"file://{resolved}"

    def _choose_path(self, local_dir: str, hwcfg: RuntimeHWConfig) -> Optional[Tuple[str, str]]:
        """ Return a deterministic path, given a parent folder and a RuntimeHWConfig object. The URI
        as generated from hwcfg is also returned. """
        uri: Optional[str] = self._resolve_vanilla_path(hwcfg)

        # do nothing if there isn't a URI
        if uri is None:
            return None

        # choose a repeatable, path based on the hash of the URI
        destination = pjoin(local_dir, self.hashed_name(uri))

        return (uri, destination)

    def local_pre_download(self, local_dir: str, hwcfg: RuntimeHWConfig) -> Optional[Tuple[str, str]]:
        """ Cached download of the URI contained in this class to a user-specified
        destination folder. The destination name is a SHA256 hash of the URI.
        If the file exists this will NOT overwrite. """

        # resolve the URI and the path '/{dir}/{hash}' we should download to
        both = self._choose_path(local_dir, hwcfg)

        # do nothing if there isn't a URI
        if both is None:
            return None

        (uri, destination) = both

        # When it exists, return the same information, but skip the download
        if Path(destination).exists():
            rootLogger.debug(f"Skipping download of uri: '{uri}'")
            return (uri, destination)

        try:
            downloadURIPrivate(uri, destination)
        except FileNotFoundError as e:
            raise Exception(f"{self.hwcfg_prop} path '{uri}' was not found")

        # return, this is not passed to rsync
        return (uri, destination)

    def get_rsync_path(self, local_dir: str, hwcfg: RuntimeHWConfig) -> Optional[Tuple[str, str]]:
        """ Does not download. Returns the rsync path required to send an already downloaded
        URI to the runhost. """

        # resolve the URI and the path '/{dir}/{hash}' we should download to
        both = self._choose_path(local_dir, hwcfg)

        # do nothing if there isn't a URI
        if both is None:
            return None

        (uri, destination) = both

        # because the local file has a nonsense name (the hash)
        # we are required to specify the destination name to rsync
        return (destination, self.destination_name)

class RuntimeHWConfig:
    """ A pythonic version of the entires in config_hwdb.yaml """
    name: str
    platform: Optional[str]

    # TODO: should be abstracted out between platforms with a URI
    agfi: Optional[str]
    """User-specified, URI path to bitstream tar file"""
    bitstream_tar: Optional[str]

    deploy_quintuplet: Optional[str]
    customruntimeconfig: str
    # note whether we've built a copy of the simulation driver for this hwconf
    driver_built: bool
    tarball_built: bool
    additional_required_files: List[Tuple[str, str]]
    driver_name_prefix: str
    local_driver_base_dir: str
    driver_type_message: str
    """User-specified, URI path to driver tarball"""
    driver_tar: Optional[str]

    """ A list of URIContainer objects, one for each URI that is able to be specified """
    uri_list: list[URIContainer]


    """ Split Stuff """
    split_config: str

    # Members that are initialized here also need to be initialized in
    # RuntimeBuildRecipeConfig.__init__
    def __init__(self, name: str, hwconfig_dict: Dict[str, Any]) -> None:
        self.name = name

        if sum(['agfi' in hwconfig_dict, 'bitstream_tar' in hwconfig_dict]) > 1:
            raise Exception(f"Must only have 'agfi' or 'bitstream_tar' HWDB entry {name}.")

        self.agfi = hwconfig_dict.get('agfi')
        self.bitstream_tar = hwconfig_dict.get('bitstream_tar')
        self.driver_tar = hwconfig_dict.get('driver_tar')

        self.platform = None
        self.driver_built = False
        self.tarball_built = False
        self.additional_required_files = []
        self.driver_name_prefix = ""
        self.driver_type_message = "FPGA software"
        self.local_driver_base_dir = LOCAL_DRIVERS_BASE

        self.uri_list = []

        self.split_config = ""

        if self.agfi is not None:
            self.platform = "f1"
        else:
            self.uri_list.append(URIContainer('bitstream_tar', self.get_bitstream_tar_filename()))

        if 'deploy_triplet_override' in hwconfig_dict.keys() and 'deploy_quintuplet_override' in hwconfig_dict.keys():
            rootLogger.error("Cannot have both 'deploy_quintuplet_override' and 'deploy_triplet_override' in hwdb entry. Define only 'deploy_quintuplet_override'.")
            sys.exit(1)
        elif 'deploy_triplet_override' in hwconfig_dict.keys():
            rootLogger.warning("Please rename your 'deploy_triplet_override' key in your hwdb entry to 'deploy_quintuplet_override'. Support for 'deploy_triplet_override' will be removed in the future.")

        hwconfig_override_build_quintuplet = hwconfig_dict.get('deploy_quintuplet_override')
        if hwconfig_override_build_quintuplet is None:
            # temporary backwards compat for old key
            hwconfig_override_build_quintuplet = hwconfig_dict.get('deploy_triplet_override')

        if hwconfig_override_build_quintuplet is not None and len(hwconfig_override_build_quintuplet.split("-")) == 3:
            # convert old build_triplet into buildquintuplet
            hwconfig_override_build_quintuplet = 'f1-firesim-' + hwconfig_override_build_quintuplet

        self.deploy_quintuplet = hwconfig_override_build_quintuplet
        if self.deploy_quintuplet is not None:
            rootLogger.warning(f"{name} is overriding a deploy quintuplet in your config_hwdb.yaml file. Make sure you understand why!")

        self.customruntimeconfig = hwconfig_dict['custom_runtime_config']

        self.additional_required_files = []

        self.uri_list.append(URIContainer('driver_tar', self.get_driver_tar_filename()))
        rootLogger.debug(f"RuntimeHWConfig self.platform {self.platform}")

    def get_deploytriplet_for_config(self) -> str:
        """ Get the deploytriplet for this configuration. """
        quin = self.get_deployquintuplet_for_config()
        return "-".join(quin.split("-")[2:])

    @classmethod
    def get_driver_tar_filename(cls) -> str:
        """ Get the name of the tarball inside the sim_slot_X directory on the run host. """
        return "driver-bundle.tar.gz"

    @classmethod
    def get_bitstream_tar_filename(cls) -> str:
        """ Get the name of the bit tar file inside the sim_slot_X directory on the run host. """
        return "firesim.tar.gz"

    def get_platform(self) -> str:
        assert self.platform is not None
        return self.platform

    def get_driver_name_suffix(self) -> str:
        return "-" + self.get_platform()

    def get_driver_build_target(self) -> str:
        return self.get_platform()

    def set_platform(self, platform: str) -> None:
        if self.platform is not None:
            assert self.platform == platform, f"platform is already set to {self.platform} cannot set it to {platform}"
        self.platform = platform

    def set_deploy_quintuplet(self, deploy_quintuplet: str) -> None:
        if self.deploy_quintuplet is not None:
            assert self.deploy_quintuplet == deploy_quintuplet, f"deploy_quintuplet is already set to {self.deploy_quintuplet} cannot set it to {deploy_quintuplet}"
        self.deploy_quintuplet = deploy_quintuplet

    def get_deployquintuplet_for_config(self) -> str:
        """ Get the deployquintuplet for this configuration. This memoizes the request
        to the AWS AGFI API."""
        rootLogger.debug(f"get_deployquintuplet_for_config {self.deploy_quintuplet} {self.get_platform}")
        if self.deploy_quintuplet is not None:
            return self.deploy_quintuplet

        if self.get_platform() == "f1":
            rootLogger.debug("Setting deployquintuplet by querying the AGFI's description.")
            self.deploy_quintuplet = get_firesim_deploy_quintuplet_for_agfi(self.agfi)
        else:
            assert False, "Unable to obtain deploy_quintuplet"

        return self.deploy_quintuplet

    def get_design_name(self) -> str:
        """ Returns the name used to prefix MIDAS-emitted files. (The DESIGN make var) """
        return self.get_deployquintuplet_for_config().split("-")[2]

    def get_local_driver_binaryname(self) -> str:
        """ Get the name of the driver binary. """
        return self.driver_name_prefix + self.get_design_name() + self.get_driver_name_suffix()

    def get_local_driver_dir(self) -> str:
        """ Get the relative local directory that contains the driver used to
        run this sim. """
        print(f"get_local_driver_dir {self.get_deployquintuplet_for_config()}")
        return self.local_driver_base_dir + "/" + self.get_platform() + "/" + self.get_deployquintuplet_for_config() + "/"

    def get_local_driver_path(self) -> str:
        """ return relative local path of the driver used to run this sim. """
        return self.get_local_driver_dir() + self.get_local_driver_binaryname()

    def local_quintuplet_path(self) -> Path:
        """ return the local path of the quintuplet folder. the tarball that is created goes inside this folder """
        quintuplet = self.get_deployquintuplet_for_config()
        return Path(get_deploy_dir()) / '../sim/output' / self.get_platform() / quintuplet

    def local_tarball_path(self, name: str) -> Path:
        """ return the local path of the tarball """
        return self.local_quintuplet_path() / name

    def get_local_runtimeconf_binaryname(self) -> str:
        """ Get the name of the runtimeconf file. """
        if self.customruntimeconfig is None:
            return None
        return os.path.basename(self.customruntimeconfig)

    def get_local_runtime_conf_path(self) -> str:
        """ return relative local path of the runtime conf used to run this sim. """
        if self.customruntimeconfig is None:
            return None
        quintuplet = self.get_deployquintuplet_for_config()
        drivers_software_base = LOCAL_DRIVERS_GENERATED_SRC + "/" + self.get_platform() + "/" + quintuplet + "/"
        return CUSTOM_RUNTIMECONFS_BASE + self.customruntimeconfig

    def get_additional_required_sim_files(self) -> List[Tuple[str, str]]:
        """ return list of any additional files required to run a simulation.
        """
        return self.additional_required_files

    def get_boot_simulation_command(self,
            slotid: int,
            all_macs: Sequence[MacAddress],
            all_rootfses: Sequence[Optional[str]],
            all_linklatencies: Sequence[int],
            all_netbws: Sequence[int],
            profile_interval: int,
            all_bootbinaries: List[str],
            all_shmemportnames: List[str],
            tracerv_config: TracerVConfig,
            autocounter_config: AutoCounterConfig,
            hostdebug_config: HostDebugConfig,
            synthprint_config: SynthPrintConfig,
            partition_config: PartitionConfig,
            tiptracing_config: TipTracingConfig,
            cutbridge_idxs: List[int],
            sudo: bool,
            extra_plusargs: str,
            extra_args: str) -> str:
        """ return the command used to boot the simulation. this has to have
        some external params passed to it, because not everything is contained
        in a runtimehwconfig. TODO: maybe runtimehwconfig should be renamed to
        pre-built runtime config? It kinda contains a mix of pre-built and
        runtime parameters currently. """

        # TODO: supernode support
        tracefile = "+tracefile=TRACEFILE" if tracerv_config.enable else ""
        autocounterfile = "+autocounter-filename-base=AUTOCOUNTERFILE"


        core_config = tiptracing_config.core_config
        tip_worker = f"+generictrace-worker={tiptracing_config.worker}" if tiptracing_config.enable else ""

        # this monstrosity boots the simulator, inside screen, inside script
        # the sed is in there to get rid of newlines in runtime confs
        driver = self.get_local_driver_binaryname()
        runtimeconf = self.get_local_runtimeconf_binaryname()

        def array_to_plusargs(valuesarr: Sequence[Optional[Any]], plusarg: str) -> List[str]:
            args = []
            for index, arg in enumerate(valuesarr):
                if arg is not None:
                    args.append("""{}{}={}""".format(plusarg, index, arg))
            return args

        def array_to_lognames(values: Sequence[Optional[Any]], prefix: str) -> List[str]:
            names = ["{}{}".format(prefix, i) if val is not None else None
                     for (i, val) in enumerate(values)]
            return array_to_plusargs(names, "+" + prefix)

        command_macs = array_to_plusargs(all_macs, "+macaddr")
        command_rootfses = array_to_plusargs(all_rootfses, "+blkdev")
        command_linklatencies = array_to_plusargs(all_linklatencies, "+linklatency")
        command_netbws = array_to_plusargs(all_netbws, "+netbw")
        command_shmemportnames = array_to_plusargs(all_shmemportnames, "+shmemportname")
        command_dromajo = "+drj_dtb=" + all_bootbinaries[0] + ".dtb" +  " +drj_bin=" + all_bootbinaries[0] + " +drj_rom=" + all_bootbinaries[0] + ".rom"

        command_niclogs = array_to_lognames(all_macs, "niclog")
        command_blkdev_logs = array_to_lognames(all_rootfses, "blkdev-log")

        command_bootbinaries = array_to_plusargs(all_bootbinaries, "+prog")
        zero_out_dram = "+zero-out-dram" if (hostdebug_config.zero_out_dram) else ""
        disable_asserts = "+disable-asserts" if (hostdebug_config.disable_synth_asserts) else ""
        print_cycle_prefix = "+print-no-cycle-prefix" if not synthprint_config.cycle_prefix else ""

        command_cutbridgeidxs = array_to_plusargs(cutbridge_idxs, "+cutbridgeidx")

        # TODO supernode support
        dwarf_file_name = "+dwarf-file-name=" + all_bootbinaries[0] + "-dwarf"

        screen_name = "fsim{}".format(slotid)

        # TODO: supernode support (tracefile, trace-select.. etc)
        permissive_driver_args = []
        permissive_driver_args += [f"$(sed \':a;N;$!ba;s/\\n/ /g\' {runtimeconf})"] if runtimeconf else []
        if profile_interval != -1:
            permissive_driver_args += [f"+profile-interval={profile_interval}"]
        permissive_driver_args += [zero_out_dram]
        permissive_driver_args += [disable_asserts]
        permissive_driver_args += command_macs
        permissive_driver_args += command_rootfses
        permissive_driver_args += command_niclogs
        permissive_driver_args += command_blkdev_logs
        permissive_driver_args += [f"{tracefile}", f"+trace-select={tracerv_config.select}", f"+trace-start={tracerv_config.start}", f"+trace-end={tracerv_config.end}", f"+trace-output-format={tracerv_config.output_format}", dwarf_file_name]
        permissive_driver_args += [f"+autocounter-readrate={autocounter_config.readrate}", autocounterfile]
        permissive_driver_args += [command_dromajo]
        permissive_driver_args += [print_cycle_prefix, f"+print-start={synthprint_config.start}", f"+print-end={synthprint_config.end}"]
        permissive_driver_args += command_linklatencies
        permissive_driver_args += command_netbws
        permissive_driver_args += command_shmemportnames
        permissive_driver_args += [f"+batch-size={self.get_init_token_cnts(partition_config)}"]
        permissive_driver_args += command_cutbridgeidxs
        permissive_driver_args += [f"{tip_worker}", f"+generictrace-core={core_config}"]

        # For QSFP metasims, assume that the partitions are connected in a ring-topology.
        # Then, when you know your FPGA idx & the total number of FPGAs, you know
        # your lhs & rhs neighbors to communicate with.
        permissive_driver_args += [f"+partition-fpga-cnt={self.get_partition_fpga_cnt()}"]
        permissive_driver_args += [f"+partition-fpga-idx={self.get_partition_fpga_idx()}"]

        driver_call = f"""./{driver} +permissive {" ".join(permissive_driver_args)} {extra_plusargs} +permissive-off {" ".join(command_bootbinaries)} {extra_args} """
        base_command = f"""script -f -c 'stty intr ^] && {driver_call} && stty intr ^c' uartlog"""
        screen_wrapped = f"""screen -S {screen_name} -d -m bash -c "{base_command}"; sleep 1"""

        return screen_wrapped

    def get_kill_simulation_command(self) -> str:
        driver = self.get_local_driver_binaryname()
        # Note that pkill only works for names <=15 characters
        return """pkill -SIGKILL {driver}""".format(driver=driver[:15])

    def handle_failure(self, buildresult: _stdoutString, what: str, dir: Path|str, cmd: str) -> None:
        """ A helper function for a nice error message when used in conjunction with the run() function"""
        if buildresult.failed:
            rootLogger.info(f"{self.driver_type_message} {what} failed. Exiting. See log for details.")
            rootLogger.info(f"""You can also re-run '{cmd}' in the '{dir}' directory to debug this error.""")
            sys.exit(1)

    def fetch_all_URI(self, dir: str) -> None:
        """ Downloads all URI. Local filenames use a hash which will be re-calculated later. Duplicate downloads
        are skipped via an exists() check on the filesystem. """
        for container in self.uri_list:
            container.local_pre_download(dir, self)

    def get_local_uri_paths(self, dir: str) -> list[Tuple[str, str]]:
        """ Get all paths of local URIs that were previously downloaded. """

        ret = list()
        for container in self.uri_list:
            maybe_file = container.get_rsync_path(dir, self)
            if maybe_file is not None:
                ret.append(maybe_file)
        return ret

    def resolve_hwcfg_values(self, dir: str) -> None:
        # must be done after fetch_all_URIs
        # based on the platform, read the URI, fill out values

        if self.platform == "f1":
            return
        else: # bitstream_tar platforms
            for container in self.uri_list:
                both = container._choose_path(dir, self)

                # do nothing if there isn't a URI
                if both is None:
                    uri = self.bitstream_tar
                    destination = self.bitstream_tar
                else:
                    (uri, destination) = both

                if uri == self.bitstream_tar and uri is not None:
                    # unpack destination value
                    temp_dir = f"{dir}/{URIContainer.hashed_name(uri)}-dir"
                    local(f"mkdir -p {temp_dir}")
                    local(f"tar xvf {destination} -C {temp_dir}")

                    # read string from metadata
                    cap = local(f"cat {temp_dir}/xilinx_alveo_u250/metadata", capture=True)
                    metadata = firesim_description_to_tags(cap)

                    self.set_platform(metadata['firesim-deployquintuplet'].split("-")[0])
                    self.set_deploy_quintuplet(metadata['firesim-deployquintuplet'])

                    break

    def get_partition_fpga_cnt(self) -> int:
        quintuplet_pieces = self.get_deployquintuplet_for_config().split("-")
        target_split_fpga_cnt  = quintuplet_pieces[5]
        return int(target_split_fpga_cnt)

    def get_partition_fpga_idx(self) -> int:
        quintuplet_pieces = self.get_deployquintuplet_for_config().split("-")
        target_split_fpga_idx = quintuplet_pieces[6]
        if (target_split_fpga_idx.isnumeric()):
            return int(target_split_fpga_idx)
        else:
            rootLogger.warning(f'FPGA index {target_split_fpga_idx} is not a number')
            return self.get_partition_fpga_cnt() - 1

    # HACK : for target preserving...
    def get_init_token_cnts(self, partition_config: PartitionConfig) -> int:
      return partition_config.batch_size

    def build_sim_driver(self) -> None:
        """ Build driver for running simulation """
        if self.driver_built:
            # we already built the driver at some point
            return
        # TODO there is a duplicate of this in runtools
        quintuplet_pieces = self.get_deployquintuplet_for_config().split("-")

        platform = quintuplet_pieces[0]
        target_project = quintuplet_pieces[1]
        design = quintuplet_pieces[2]
        target_config = quintuplet_pieces[3]
        platform_config = quintuplet_pieces[4]
        target_split_fpga_cnt  = quintuplet_pieces[5]
        target_split_idx = quintuplet_pieces[6]

        rootLogger.info(f"Building {self.driver_type_message} driver for {str(self.get_deployquintuplet_for_config())}")

        with InfoStreamLogger('stdout'), prefix(f'cd {get_deploy_dir()}/../'), \
            prefix(f'export RISCV={os.getenv("RISCV", "")}'), \
            prefix(f'export PATH={os.getenv("PATH", "")}'), \
            prefix(f'export LD_LIBRARY_PATH={os.getenv("LD_LIBRARY_PATH", "")}'), \
            prefix('source sourceme-manager.sh --skip-ssh-setup'), \
            prefix('cd sim/'):
            driverbuildcommand = f"make PLATFORM={self.get_platform()} TARGET_PROJECT={target_project} DESIGN={design} TARGET_CONFIG={target_config} PLATFORM_CONFIG={platform_config} TARGET_SPLIT_FPGA_CNT={target_split_fpga_cnt} TARGET_SPLIT_IDX={target_split_idx} {self.get_driver_build_target()}"
            buildresult = run(driverbuildcommand)
            self.handle_failure(buildresult, 'driver build', 'firesim/sim', driverbuildcommand)

        self.driver_built = True

    def build_sim_tarball(self, paths: List[Tuple[str, str]], tarball_name: str) -> None:
        """ Take the simulation driver and tar it. build_sim_driver()
        must run before this function.  Rsync is used in a mode where it's copying
        from local paths to a local folder. This is confusing as rsync traditionally is
        used for copying from local folders to a remote folder. The variable local_remote_dir is
        named as a reminder that it's actually pointing at this local machine"""
        if self.tarball_built:
            # we already built it
            return

        # builddir is a temporary directory created by TemporaryDirectory()
        # the path a folder is under /tmp/ with a random name
        # After this scope block exists, the entire folder is deleted
        with TemporaryDirectory() as builddir:

            with InfoStreamLogger('stdout'), prefix(f'cd {get_deploy_dir()}'):
                for local_path, remote_path in paths:
                    # The `rsync_project()` function does not allow
                    # copying between two local directories.
                    # This uses the same option flags but operates rsync in local->local mode
                    options = '-pthrvz -L'
                    local_dir = local_path
                    local_remote_dir = pjoin(builddir, remote_path)
                    cmd = f"rsync {options} {local_dir} {local_remote_dir}"

                    results = run(cmd)
                    self.handle_failure(results, 'local rsync', get_deploy_dir(), cmd)

            # This must be taken outside of a cd context
            cmd = f"mkdir -p {self.local_quintuplet_path()}"
            results = run(cmd)
            self.handle_failure(results, 'local mkdir', builddir, cmd)
            absolute_tarball_path = self.local_quintuplet_path() / tarball_name

            with InfoStreamLogger('stdout'), prefix(f'cd {builddir}'):
                findcmd = 'find . -mindepth 1 -maxdepth 1 -printf "%P\n"'
                taroptions = '-czvf'

                # Running through find and xargs is the most simple way I've found to meet these requirements:
                #   * create the tar with no leading ./ or foldername
                #   * capture all types of hidden files (.a ..a .aa)
                #   * avoid capturing the parent folder (..) with globs looking for hidden files
                cmd = f"{findcmd} | xargs tar {taroptions} {absolute_tarball_path}"

                results = run(cmd)
                self.handle_failure(results, 'tarball', builddir, cmd)

            self.tarball_built = True

    def __str__(self) -> str:
        return """RuntimeHWConfig: {}\nDeployQuintuplet: {}\nAGFI: {}\nBitstream tar: {}\nCustomRuntimeConf: {}""".format(self.name, self.deploy_quintuplet, self.agfi, self.bitstream_tar, str(self.customruntimeconfig))




class RuntimeBuildRecipeConfig(RuntimeHWConfig):
    """ A pythonic version of the entires in config_build_recipes.yaml """

    def __init__(self, name: str, build_recipe_dict: Dict[str, Any],
                 default_metasim_host_sim: str,
                 metasimulation_only_plusargs: str,
                 metasimulation_only_vcs_plusargs: str) -> None:
        self.name = name

        self.agfi = None
        self.bitstream_tar = None
        self.driver_tar = None
        self.tarball_built = False

        self.uri_list = []

        self.split_config = str(build_recipe_dict.get('TARGET_SPLIT_CONFIG'))

        self.deploy_quintuplet = build_recipe_dict.get('PLATFORM', 'f1') + "-" + build_recipe_dict.get('TARGET_PROJECT', 'firesim') + "-" + build_recipe_dict['DESIGN'] + "-" + build_recipe_dict['TARGET_CONFIG'] + "-" + build_recipe_dict['PLATFORM_CONFIG'] + "-" + self.split_config

        self.customruntimeconfig = build_recipe_dict['metasim_customruntimeconfig']
        # note whether we've built a copy of the simulation driver for this hwconf
        self.driver_built = False
        self.metasim_host_simulator = default_metasim_host_sim

        self.platform = build_recipe_dict.get('PLATFORM', 'f1')
        self.driver_name_prefix = ""
        if self.metasim_host_simulator in ["verilator", "verilator-debug"]:
            self.driver_name_prefix = "V"

        self.local_driver_base_dir = LOCAL_DRIVERS_GENERATED_SRC

        self.driver_type_message = "Metasim"

        self.metasimulation_only_plusargs = metasimulation_only_plusargs
        self.metasimulation_only_vcs_plusargs = metasimulation_only_vcs_plusargs

        self.additional_required_files = []

        if self.metasim_host_simulator in ["vcs", "vcs-debug"]:
            self.additional_required_files.append((self.get_local_driver_path() + ".daidir", ""))

    def get_driver_name_suffix(self) -> str:
        driver_name_suffix = ""
        if self.metasim_host_simulator in ['verilator-debug', 'vcs-debug']:
            driver_name_suffix = "-debug"
        return driver_name_suffix

    def get_driver_build_target(self) -> str:
        return self.metasim_host_simulator

    def get_boot_simulation_command(self,
            slotid: int,
            all_macs: Sequence[MacAddress],
            all_rootfses: Sequence[Optional[str]],
            all_linklatencies: Sequence[int],
            all_netbws: Sequence[int],
            profile_interval: int,
            all_bootbinaries: List[str],
            all_shmemportnames: List[str],
            tracerv_config: TracerVConfig,
            autocounter_config: AutoCounterConfig,
            hostdebug_config: HostDebugConfig,
            synthprint_config: SynthPrintConfig,
            partition_config: PartitionConfig,
            tiptracing_config: TipTracingConfig,
            cutbridge_idxs: List[int],
            sudo: bool,
            extra_plusargs: str,
            extra_args: str) -> str:
        """ return the command used to boot the meta simulation. """
        full_extra_plusargs = " " + self.metasimulation_only_plusargs + " " + extra_plusargs
        if self.metasim_host_simulator in ['vcs', 'vcs-debug']:
            full_extra_plusargs = " " + self.metasimulation_only_vcs_plusargs + " " +  full_extra_plusargs
        if self.metasim_host_simulator == 'verilator-debug':
            full_extra_plusargs += " +waveformfile=metasim_waveform.vcd "
        if self.metasim_host_simulator == 'vcs-debug':
            full_extra_plusargs += " +fsdbfile=metasim_waveform.fsdb "
        # TODO: spike-dasm support
        full_extra_args = " 2> metasim_stderr.out " + extra_args
        return super(RuntimeBuildRecipeConfig, self).get_boot_simulation_command(
            slotid,
            all_macs,
            all_rootfses,
            all_linklatencies,
            all_netbws,
            profile_interval,
            all_bootbinaries,
            all_shmemportnames,
            tracerv_config,
            autocounter_config,
            hostdebug_config,
            synthprint_config,
            partition_config,
            tiptracing_config,
            cutbridge_idxs,
            sudo,
            full_extra_plusargs,
            full_extra_args)

class RuntimeHWDB:
    """ This class manages the hardware configurations that are available
    as endpoints on the simulation. """
    hwconf_dict: Dict[str, RuntimeHWConfig]
    config_file_name: str
    simulation_mode_string: str

    def __init__(self, hardwaredbconfigfile: str) -> None:
        self.config_file_name = hardwaredbconfigfile
        self.simulation_mode_string = "FPGA simulation"

        agfidb_configfile = None
        with open(hardwaredbconfigfile, "r") as yaml_file:
            agfidb_configfile = yaml.safe_load(yaml_file)

        agfidb_dict = agfidb_configfile

        self.hwconf_dict = {s: RuntimeHWConfig(s, v) for s, v in agfidb_dict.items()}

    def keyerror_message(self, name: str) -> str:
        """ Return the error message for lookup errors."""
        return f"'{name}' not found in '{self.config_file_name}', which is used to specify target design descriptions for {self.simulation_mode_string}s."

    def get_runtimehwconfig_from_name(self, name: str) -> RuntimeHWConfig:
        if name not in self.hwconf_dict:
            raise KeyError(self.keyerror_message(name))
        return self.hwconf_dict[name]

    def __str__(self) -> str:
        return pprint.pformat(vars(self))

class RuntimeBuildRecipes(RuntimeHWDB):
    """ Same as RuntimeHWDB, but use information from build recipes entries
    instead of hwdb for metasimulation."""

    def __init__(self, build_recipes_config_file: str,
                 metasim_host_simulator: str,
                 metasimulation_only_plusargs: str,
                 metasimulation_only_vcs_plusargs: str) -> None:
        self.config_file_name = build_recipes_config_file
        self.simulation_mode_string = "Metasimulation"

        recipes_configfile = None
        with open(build_recipes_config_file, "r") as yaml_file:
            recipes_configfile = yaml.safe_load(yaml_file)

        recipes_dict = recipes_configfile

        self.hwconf_dict = {s: RuntimeBuildRecipeConfig(s, v, metasim_host_simulator, metasimulation_only_plusargs, metasimulation_only_vcs_plusargs) for s, v in recipes_dict.items()}


class InnerRuntimeConfiguration:
    """ Pythonic version of config_runtime.yaml """
    run_farm_requested_name: str
    run_farm_dispatcher: RunFarm
    topology: str
    no_net_num_nodes: int
    linklatency: int
    switchinglatency: int
    netbandwidth: int
    profileinterval: int
    launch_timeout: timedelta
    always_expand: bool
    tracerv_config: TracerVConfig
    autocounter_config: AutoCounterConfig
    hostdebug_config: HostDebugConfig
    synthprint_config: SynthPrintConfig
    partition_config: PartitionConfig
    tiptrace_config: TipTracingConfig
    workload_name: str
    suffixtag: str
    terminateoncompletion: bool
    metasimulation_enabled: bool
    metasimulation_host_simulator: str
    metasimulation_only_plusargs: str
    metasimulation_only_vcs_plusargs: str
    default_plusarg_passthrough: str

    def __init__(self, runtimeconfigfile: str, configoverridedata: str) -> None:

        runtime_configfile = None
        with open(runtimeconfigfile, "r") as yaml_file:
            runtime_configfile = yaml.safe_load(yaml_file)

        runtime_dict = runtime_configfile

        # override parts of the runtime conf if specified
        if configoverridedata != "":
            ## handle overriding part of the runtime conf
            configoverrideval = configoverridedata.split()
            overridesection = configoverrideval[0]
            overridefield = configoverrideval[1]
            overridevalue = configoverrideval[2]
            rootLogger.warning("Overriding part of the runtime config with: ")
            rootLogger.warning("""[{}]""".format(overridesection))
            rootLogger.warning(overridefield + "=" + overridevalue)
            runtime_dict[overridesection][overridefield] = overridevalue

        def dict_assert(key_check, dict_name):
            assert key_check in dict_name, f"FAIL: missing {key_check} in runtime config."

        dict_assert('metasimulation', runtime_dict)
        metasim_dict = runtime_dict['metasimulation']
        dict_assert('metasimulation_enabled', metasim_dict)
        self.metasimulation_enabled = metasim_dict['metasimulation_enabled']
        dict_assert('metasimulation_host_simulator', metasim_dict)
        self.metasimulation_host_simulator = metasim_dict['metasimulation_host_simulator']
        dict_assert('metasimulation_only_plusargs', metasim_dict)
        self.metasimulation_only_plusargs = metasim_dict['metasimulation_only_plusargs']
        dict_assert('metasimulation_only_vcs_plusargs', metasim_dict)
        self.metasimulation_only_vcs_plusargs = metasim_dict['metasimulation_only_vcs_plusargs']

        # Setup the run farm
        defaults_file = runtime_dict['run_farm']['base_recipe']
        with open(defaults_file, "r") as yaml_file:
            run_farm_configfile = yaml.safe_load(yaml_file)
        run_farm_type = run_farm_configfile["run_farm_type"]
        run_farm_args = run_farm_configfile["args"]

        # add the overrides if it exists

        override_args = runtime_dict['run_farm'].get('recipe_arg_overrides')
        if override_args:
            run_farm_args = deep_merge(run_farm_args, override_args)

        run_farm_dispatch_dict = dict([(x.__name__, x) for x in inheritors(RunFarm)])

        if not run_farm_type in run_farm_dispatch_dict:
            raise Exception(f"Unable to find {run_farm_type} in available run farm classes: {run_farm_dispatch_dict.keys()}")

        # create dispatcher object using class given and pass args to it
        self.run_farm_dispatcher = run_farm_dispatch_dict[run_farm_type](run_farm_args, self.metasimulation_enabled)

        self.topology = runtime_dict['target_config']['topology']
        self.no_net_num_nodes = int(runtime_dict['target_config']['no_net_num_nodes'])
        self.linklatency = int(runtime_dict['target_config']['link_latency'])
        self.switchinglatency = int(runtime_dict['target_config']['switching_latency'])
        self.netbandwidth = int(runtime_dict['target_config']['net_bandwidth'])
        self.profileinterval = int(runtime_dict['target_config']['profile_interval'])
        self.defaulthwconfig = runtime_dict['target_config']['default_hw_config']

        self.tracerv_config = TracerVConfig(runtime_dict.get('tracing', {}))
        self.autocounter_config = AutoCounterConfig(runtime_dict.get('autocounter', {}))
        self.hostdebug_config = HostDebugConfig(runtime_dict.get('host_debug', {}))
        self.synthprint_config = SynthPrintConfig(runtime_dict.get('synth_print', {}))
        self.partition_config = PartitionConfig(runtime_dict.get('partitioning', {}))
        self.tiptrace_config = TipTracingConfig(runtime_dict.get('tip_tracing', {}))

        dict_assert('plusarg_passthrough', runtime_dict['target_config'])
        self.default_plusarg_passthrough = runtime_dict['target_config']['plusarg_passthrough']

        self.workload_name = runtime_dict['workload']['workload_name']
        # an extra tag to differentiate workloads with the same name in results names
        self.suffixtag = runtime_dict['workload']['suffix_tag'] if 'suffix_tag' in runtime_dict['workload'] else None
        self.terminateoncompletion = runtime_dict['workload']['terminate_on_completion'] == True

    def __str__(self) -> str:
        return pprint.pformat(vars(self))

class RuntimeConfig:
    """ This class manages the overall configuration of the manager for running
    simulation tasks. """
    launch_time: str
    args: argparse.Namespace
    runtimehwdb: RuntimeHWDB
    innerconf: InnerRuntimeConfiguration
    run_farm: RunFarm
    workload: WorkloadConfig
    firesim_topology_with_passes: FireSimTopologyWithPasses
    runtime_build_recipes: RuntimeBuildRecipes

    def __init__(self, args: argparse.Namespace) -> None:
        """ This reads runtime configuration files, massages them into formats that
        the rest of the manager expects, and keeps track of other info. """
        self.launch_time = strftime("%Y-%m-%d--%H-%M-%S", gmtime())

        self.args = args

        # construct pythonic db of hardware configurations available to us at
        # runtime.
        self.runtimehwdb = RuntimeHWDB(args.hwdbconfigfile)
        rootLogger.debug(self.runtimehwdb)

        self.innerconf = InnerRuntimeConfiguration(args.runtimeconfigfile,
                                                   args.overrideconfigdata)
        rootLogger.debug(self.innerconf)

        self.runtime_build_recipes = RuntimeBuildRecipes(args.buildrecipesconfigfile, self.innerconf.metasimulation_host_simulator, self.innerconf.metasimulation_only_plusargs, self.innerconf.metasimulation_only_vcs_plusargs)
        rootLogger.debug(self.runtime_build_recipes)

        self.run_farm = self.innerconf.run_farm_dispatcher

        # setup workload config obj, aka a list of workloads that can be assigned
        # to a server
        if args.task != 'enumeratefpgas':
            self.workload = WorkloadConfig(self.innerconf.workload_name, self.launch_time,
                                           self.innerconf.suffixtag)
        else:
            self.workload = WorkloadConfig('dummy.json', self.launch_time,
                                           self.innerconf.suffixtag)

        # start constructing the target configuration tree
        self.firesim_topology_with_passes = FireSimTopologyWithPasses(
            self.innerconf.topology, self.innerconf.no_net_num_nodes,
            self.run_farm, self.runtimehwdb, self.innerconf.defaulthwconfig,
            self.workload, self.innerconf.linklatency,
            self.innerconf.switchinglatency, self.innerconf.netbandwidth,
            self.innerconf.profileinterval,
            self.innerconf.tracerv_config,
            self.innerconf.autocounter_config,
            self.innerconf.hostdebug_config,
            self.innerconf.synthprint_config,
            self.innerconf.partition_config,
            self.innerconf.tiptrace_config,
            self.innerconf.terminateoncompletion,
            self.runtime_build_recipes,
            self.innerconf.metasimulation_enabled,
            self.innerconf.default_plusarg_passthrough)

    def launch_run_farm(self) -> None:
        """ directly called by top-level launchrunfarm command. """
        self.run_farm.launch_run_farm()

    def terminate_run_farm(self) -> None:
        """ directly called by top-level terminaterunfarm command. """
        terminate_some_dict = {}
        if self.args.terminatesome is not None:
            for pair in self.args.terminatesome:
                terminate_some_dict[pair[0]] = pair[1]

        def old_style_terminate_args(instance_type, arg_val, arg_flag_str):
            if arg_val != -1:
                rootLogger.critical("WARNING: You are using the old-style " + arg_flag_str + " flag. See the new --terminatesome flag in help. The old-style flag will be removed in the next major FireSim release (1.15.X).")
                terminate_some_dict[instance_type] = arg_val

        old_style_terminate_args('f1.16xlarge', self.args.terminatesomef116, '--terminatesomef116')
        old_style_terminate_args('f1.4xlarge', self.args.terminatesomef14, '--terminatesomef14')
        old_style_terminate_args('f1.2xlarge', self.args.terminatesomef12, '--terminatesomef12')
        old_style_terminate_args('m4.16xlarge', self.args.terminatesomem416, '--terminatesomem416')

        self.run_farm.terminate_run_farm(terminate_some_dict, self.args.forceterminate)

    def infrasetup(self) -> None:
        """ directly called by top-level infrasetup command. """
        # set this to True if you want to use mock boto3 instances for testing
        # the manager.
        use_mock_instances_for_testing = False
        self.firesim_topology_with_passes.infrasetup_passes(use_mock_instances_for_testing)

    def build_driver(self) -> None:
        """ directly called by top-level builddriver command. """
        self.firesim_topology_with_passes.build_driver_passes()

    def enumerate_fpgas(self) -> None:
        """ directly called by top-level enumeratefpgas command. """
        use_mock_instances_for_testing = False
        self.firesim_topology_with_passes.enumerate_fpgas_passes(use_mock_instances_for_testing)

    def boot(self) -> None:
        """ directly called by top-level boot command. """
        use_mock_instances_for_testing = False
        self.firesim_topology_with_passes.boot_simulation_passes(use_mock_instances_for_testing)

    def kill(self) -> None:
        use_mock_instances_for_testing = False
        self.firesim_topology_with_passes.kill_simulation_passes(use_mock_instances_for_testing)

    def run_workload(self) -> None:
        use_mock_instances_for_testing = False
        self.firesim_topology_with_passes.run_workload_passes(use_mock_instances_for_testing)
