# Copyright 2015 Red Hat, Inc.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import os
import shlex
import shutil
import tempfile
import time

from oslo_concurrency import processutils
from oslo_log import log

from ironic_python_agent import errors
from ironic_python_agent.extensions import base
from ironic_python_agent.extensions import iscsi
from ironic_python_agent import hardware
from ironic_python_agent import utils

LOG = log.getLogger(__name__)


BIND_MOUNTS = ('/dev', '/proc')


def _get_partition(device, uuid):
    """Find the partition of a given device."""
    LOG.info("Find the partition %(uuid)s on device %(dev)s",
              {'dev': device, 'uuid': uuid})

    try:
        # Try to tell the kernel to re-read the partition table
        try:
            utils.execute('partx', '-u', device, attempts=3,
                          delay_on_retry=True)
            utils.execute('udevadm', 'settle')
        except processutils.ProcessExecutionError:
            LOG.warning("Couldn't re-read the partition table "
                        "on device %s", device)

        # FIXME! If we get the partition ID of the md/p1 device, we should
        # be able to find the partition rather than hardcoding it ...
        if _is_md_device(device):
            LOG.warning("FIXME: _get_partition() returns hard-coded partition")
            return device + 'p1'
        LOG.error("_get_partition() %s is not an md device", device)

        lsblk = utils.execute('lsblk', '-PbioKNAME,UUID,PARTUUID,TYPE', device)
        report = lsblk[0]

        for line in report.split('\n'):
            part = {}
            # Split into KEY=VAL pairs
            vals = shlex.split(line)
            for key, val in (v.split('=', 1) for v in vals):
                part[key] = val.strip()
            # Ignore non partition
            if part.get('TYPE') != 'part':
                continue

            if part.get('UUID') == uuid:
                LOG.debug("Partition %(uuid)s found on device "
                          "%(dev)s", {'uuid': uuid, 'dev': device})
                return '/dev/' + part.get('KNAME')
            if part.get('PARTUUID') == uuid:
                LOG.debug("Partition %(uuid)s found on device "
                          "%(dev)s", {'uuid': uuid, 'dev': device})
                return '/dev/' + part.get('KNAME')
        else:
            error_msg = ("No partition with UUID %(uuid)s found on "
                         "device %(dev)s" % {'uuid': uuid, 'dev': device})
            LOG.error(error_msg)
            raise errors.DeviceNotFound(error_msg)
    except processutils.ProcessExecutionError as e:
        error_msg = ('Finding the partition with UUID %(uuid)s on '
                     'device %(dev)s failed with %(err)s' %
                     {'uuid': uuid, 'dev': device, 'err': e})
        LOG.error(error_msg)
        raise errors.CommandExecutionError(error_msg)


def _is_md_device(device):
    """Check if device is an md device"""
    try:
        utils.execute("mdadm --detail {}".format(device), shell=True)
        LOG.info("%s is an md device", device)
        return True
    except processutils.ProcessExecutionError:
        LOG.info("%s is not an md device", device)
        return False
 

def _md_get_components(device):
    """Return the components of an md device"""
    try:
        cmd = ("mdadm --detail {}|"
               "sed -e '1,/Number   Major/d'|"
               "awk '{{print $NF}}'")
        out, err = utils.execute(cmd.format(device), shell=True)
    except processutils.ProcessExecutionError as e:
        error_msg = ('Cannot get components of md device %(dev)s: %(err)s' %
                     {'dev': device, 'err': e})
        LOG.error(error_msg)
        raise errors.CommandExecutionError(error_msg)
    LOG.info("Components of %(dev)s: %(comp)s" %
             {'dev': device, 'comp': ' '.join(out.splitlines())})
    return out.splitlines()
 

def _md_get_holders(device):
    """Return the underlying disks for an md device"""
    try:
        cmd = ("mdadm --detail {}|"
               "sed -e '1,/Number   Major/d'|"
               "awk '{{print $NF}}'|"
               "sed 's/[0-9]//g'")
        out, err = utils.execute(cmd.format(device), shell=True)
    except processutils.ProcessExecutionError as e:
        error_msg = ('Cannot get holders of md device %(dev)s: %(err)s' % 
                     {'dev': device, 'err': e})
        LOG.error(error_msg)
        raise errors.CommandExecutionError(error_msg)
    LOG.info("Holders of %(dev)s: %(comp)s" %
             {'dev': device, 'comp': ' '.join(out.splitlines())})
    return out.splitlines()
 

def _md_restart(device):
    """Restart md device"""
    try:
        components = _md_get_components(device) 
        utils.execute("mdadm --stop {}".format(device), shell=True)
        utils.execute("mdadm --assemble {} {}".format(device, ' '.join(components)),
                      shell=True)
    except processutils.ProcessExecutionError as e:
        error_msg = ('Could not restart md device %(dev)s: %(err)s' %
                     {'dev': device, 'err': e})
        LOG.error(error_msg)
        raise errors.CommandExecutionError(error_msg)       


def _install_grub2(device, root_uuid, efi_system_part_uuid=None,
                   prep_boot_part_uuid=None):
    """Install GRUB2 bootloader on a given device."""
    LOG.debug("Installing GRUB2 bootloader on device %s", device)
    root_partition = _get_partition(device, uuid=root_uuid)
    efi_partition = None
    efi_partition_mount_point = None
    efi_mounted = False

    try:
        # Mount the partition and binds
        path = tempfile.mkdtemp()

        if efi_system_part_uuid:
            efi_partition = _get_partition(device, uuid=efi_system_part_uuid)
            efi_partition_mount_point = os.path.join(path, "boot/efi")

        # For power we want to install grub directly onto the PreP partition
        if prep_boot_part_uuid:
            device = _get_partition(device, uuid=prep_boot_part_uuid)

        # If the root device is an md device (or partition), we need to
        # restart the device to help grub find it and identify  the underlying
        # disks to install grub
        if _is_md_device(device):
            _md_restart(device)
            disks = _md_get_holders(device)
        else:
            disks.append(device)

        LOG.info("Mount %(root_partition)s into %(path)s" %
                 {'root_partition': root_partition, 'path': path})
        utils.execute('mount', root_partition, path)
        for fs in BIND_MOUNTS:
            utils.execute('mount', '-o', 'bind', fs, path + fs)

        utils.execute('mount', '-t', 'sysfs', 'none', path + '/sys')

        if efi_partition:
            if not os.path.exists(efi_partition_mount_point):
                os.makedirs(efi_partition_mount_point)
            utils.execute('mount', efi_partition, efi_partition_mount_point)
            efi_mounted = True

        binary_name = "grub"
        if os.path.exists(os.path.join(path, 'usr/sbin/grub2-install')):
            binary_name = "grub2"

        # Add /bin to PATH variable as grub requires it to find efibootmgr
        # when running in uefi boot mode.
        # Add /usr/sbin to PATH variable to ensure it is there as we do
        # not use full path to grub binary anymore.
        path_variable = os.environ.get('PATH', '')
        path_variable = '%s:/bin:/usr/sbin' % path_variable

        # Install grub
        LOG.info("GRUB2 will be installed on disks %s", disks)
        for grub_disk in disks:
            LOG.info("Installing on grub_disk %s", grub_disk)
            utils.execute('chroot %(path)s /bin/sh -c '
                          '"%(bin)s-install %(dev)s"' %
                          {'path': path, 'bin': binary_name,
                           'dev': grub_disk},
                          shell=True, env_variables={'PATH': path_variable})
            LOG.info("GRUB2 successfully installed on device %s", grub_disk)
        # Also run grub-install with --removable, this installs grub to the
        # EFI fallback path. Useful if the NVRAM wasn't written correctly,
        # was reset or if testing with virt as libvirt resets the NVRAM
        # on instance start.
        # This operation is essentially a copy operation. Use of the
        # --removable flag, per the grub-install source code changes
        # the default file to be copied, destination file name, and
        # prevents NVRAM from being updated.
        if efi_partition:
            utils.execute('chroot %(path)s /bin/sh -c '
                          '"%(bin)s-install %(dev)s --removable"' %
                          {'path': path, 'bin': binary_name, 'dev': device},
                          shell=True, env_variables={'PATH': path_variable})

        # Generate the grub configuration file
        utils.execute('chroot %(path)s /bin/sh -c '
                      '"%(bin)s-mkconfig -o '
                      '/boot/%(bin)s/grub.cfg"' %
                      {'path': path, 'bin': binary_name}, shell=True,
                      env_variables={'PATH': path_variable})

        LOG.info("GRUB2 successfully installed for root device %s", device)

    except processutils.ProcessExecutionError as e:
        error_msg = ('Installing GRUB2 boot loader to device %(dev)s '
                     'failed with %(err)s.' % {'dev': device, 'err': e})
        LOG.error(error_msg)
       
        # arne 10SEP2018: wait until /tmp/ipa-done exists 
        while not os.path.isfile("/tmp/ipa-done"):
            LOG.info("Waiting until /tmp/ipa-done exists ...")
            time.sleep(10)

        # arne 11SEP2018: return success so that the machine can be
        # debugged using a live image
        # raise errors.CommandExecutionError(error_msg)

    finally:
        umount_warn_msg = "Unable to umount %(path)s. Error: %(error)s"
        # Umount binds and partition
        umount_binds_fail = False

        # If umount fails for efi partition, then we cannot be sure that all
        # the changes were written back to the filesystem.
        try:
            if efi_mounted:
                utils.execute('umount', efi_partition_mount_point, attempts=3,
                              delay_on_retry=True)
        except processutils.ProcessExecutionError as e:
            error_msg = ('Umounting efi system partition failed. '
                         'Attempted 3 times. Error: %s' % e)
            LOG.error(error_msg)
            raise errors.CommandExecutionError(error_msg)

        for fs in BIND_MOUNTS:
            try:
                utils.execute('umount', path + fs, attempts=3,
                              delay_on_retry=True)
            except processutils.ProcessExecutionError as e:
                umount_binds_fail = True
                LOG.warning(umount_warn_msg, {'path': path + fs, 'error': e})

        try:
            utils.execute('umount', path + '/sys', attempts=3,
                          delay_on_retry=True)
        except processutils.ProcessExecutionError as e:
            umount_binds_fail = True
            LOG.warning(umount_warn_msg, {'path': path + '/sys', 'error': e})

        # If umounting the binds succeed then we can try to delete it
        if not umount_binds_fail:
            try:
                utils.execute('umount', path, attempts=3, delay_on_retry=True)
            except processutils.ProcessExecutionError as e:
                LOG.warning(umount_warn_msg, {'path': path, 'error': e})
            else:
                # After everything is umounted we can then remove the
                # temporary directory
                shutil.rmtree(path)


class ImageExtension(base.BaseAgentExtension):

    @base.sync_command('install_bootloader')
    def install_bootloader(self, root_uuid, efi_system_part_uuid=None,
                           prep_boot_part_uuid=None):
        """Install the GRUB2 bootloader on the image.

        :param root_uuid: The UUID of the root partition.
        :param efi_system_part_uuid: The UUID of the efi system partition.
            To be used only for uefi boot mode.  For uefi boot mode, the
            boot loader will be installed here.
        :param prep_boot_part_uuid: The UUID of the PReP Boot partition.
            Used only for booting ppc64* partition images locally. In this
            scenario the bootloader will be installed here.
        :raises: CommandExecutionError if the installation of the
                 bootloader fails.
        :raises: DeviceNotFound if the root partition is not found.

        """
        device = hardware.dispatch_to_managers('get_os_install_device')
        iscsi.clean_up(device)
        _install_grub2(device,
                       root_uuid=root_uuid,
                       efi_system_part_uuid=efi_system_part_uuid,
                       prep_boot_part_uuid=prep_boot_part_uuid)
