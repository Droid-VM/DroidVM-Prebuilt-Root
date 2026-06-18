#!/bin/bash
set -o pipefail
mkdir -pv /vars
if ! mount -t virtiofs vars /vars; then
        if ! mount -t 9p -o trans=virtio vars /vars; then
                echo "Failed to mount vars filesystem for agent"
                exit 1
        fi
fi
echo "Mounted vars filesystem at /vars"
if ! [ -f /vars/actions.txt ]; then
	echo "/vars/actions.txt not found"
	exit 1
fi
source /vars/actions.txt
rm -fv /vars/result.txt
echo "STARTED=true" >> /vars/result.txt
if [ "$MOUNT_ROOT" == true ]; then
	echo "Probing root"
	mkdir -pv /mnt
	TARGET_DEVICE=""
	for dev in $(blkid | grep -E 'TYPE="(ext4|btrfs|xfs|f2fs)"' | awk -F: '{print $1}'); do
		echo "Try probe device $dev"
		if ! mount -v "$dev" /mnt; then
			echo "Failed mount device $dev"
			continue
		fi
		if [ -f /mnt/etc/passwd ]; then
			echo "Found target device $dev"
			TARGET_DEVICE="$dev"
			break
		fi
		umount /mnt
	done
	if ! [ -b "$TARGET_DEVICE" ]; then
		echo "No root found"
		echo "ROOT_NOT_FOUND=true" >> /vars/result.txt
		exit 1
	fi
	echo "ROOT_FOUND=true" >> /vars/result.txt
	echo "ROOT_DEVICE=$TARGET_DEVICE" >> /vars/result.txt
fi
if [ "$ACTION" == passwd ]; then
	echo "Change password..."
	function list_normal_users() {
		awk -F: '$3 >= 1000 && $3 < 2000 {print $3" "$1}' /mnt/etc/passwd | sort -n | awk '{print $2}'
	}
	function change_password() {
		echo "Change password for $1"
		if ! echo "$1:$PASSWORD" | busybox chpasswd --crypt-method SHA256 --root /mnt; then
			echo "Change password for $1 failed"
			echo "PASSWD_FAILED=true" >> /vars/result.txt
			exit 1
		fi
	}
	change_password root
	if [ "$PASSWD_NORMAL_USERS" == true ]; then
		for user in $(list_normal_users); do
			change_password "$user"
			if [ "$PASSWD_NORMAL_USER_ONE" == true ]; then
				break
			fi
		done
	fi
	echo "PASSWD_SUCCESS=true" >> /vars/result.txt
	echo "Change password done"
fi
echo "ALL_SUCCESS=true" >> /vars/result.txt
if mountpoint -q /mnt; then
	umount /mnt
fi
umount /vars
echo "Agent script success"
