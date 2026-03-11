#!/usr/bin/env bash
#
# Copyright (C) 2022 Ing <https://github.com/wjz304>
#
# This is free software, licensed under the MIT License.
# See /LICENSE for more information.
#

log() {
  echo "[vmctl] $*"
}

err() {
  echo "[vmctl][error] $*" >&2
}

die() {
  err "$*"
  exit 1
}

usage() {
  cat <<'EOF'
Usage: vmctl <command> [options]

Environment Variables:
  TRIM_OVERLAYFS
    path to overlayfs directory, default /var/apps/fn-linux-station/var/trim_overlayfs.

Commands:
  -i, --install [-r|--install-recommends <yes|no>] [-z|--install-chinese <yes|no>] [-c|--install-commonly <yes|no>]
      export this system to overlayfs directory.
      -r, --install-recommends <yes|no>    Install recommends packages
      -z, --install-chinese <yes|no>       Install chinese packages
      -c, --install-commonly <yes|no>      Install 3rd packages

  -u, --uninstall
      delete overlayfs directory.

  -s, --start [-s|--single-port <port>] [-m|--mirror-port <port>] [-p|--novnc-passwd <passwd>]
      start the chroot environment.
      -s, --single-port <port>     port number for single desktop noVNC connection, default 6080.
      -m, --mirror-port <port>     port number for mirror desktop noVNC connection, default 6081.
      -p, --novnc-passwd <passwd>  password for noVNC, default passwd.

  -p, --stop
      stop the chroot environment.

  -t, --status
      check the status of the chroot environment.

  -r, --run <command...>
      run a command in the chroot environment.

  -h, --help
      display help information.
EOF
}

TRIM_PKGVAR="/var/apps/fn-linux-station/var"
TRIM_OVERLAYFS="${TRIM_OVERLAYFS:-${TRIM_PKGVAR}/trim_overlayfs}"

resolve_overlayfs() {
  local abs_overlayfs
  abs_overlayfs="$(realpath -m -- "${TRIM_OVERLAYFS:-}" 2>/dev/null)"
  [ -n "${abs_overlayfs:-}" ] || die "TRIM_OVERLAYFS environment variable is not set."
  [ "${abs_overlayfs:-}" = "/" ] && die "Refusing to use / as overlayfs path."
  printf '%s\n' "${abs_overlayfs:-}"
}

mount_overlayfs() {
  local abs_overlayfs="${1}"
  # Create necessary directories for OverlayFS
  mkdir -p "${abs_overlayfs:-}"/{lower,upper,work,merged} #|| die "Creating OverlayFS directories failed: ${abs_overlayfs:-}"
  # Mount the OverlayFS
  mountpoint -q "${abs_overlayfs:-}/merged" || mount -t overlay overlay -o "rw,relatime,lowerdir=/,upperdir=${abs_overlayfs:-}/upper,workdir=${abs_overlayfs:-}/work" "${abs_overlayfs:-}/merged" || die "Mounting OverlayFS failed: ${abs_overlayfs:-}/merged"
}

unmount_overlayfs() {
  local abs_overlayfs="${1}"
  mountpoint -q "${abs_overlayfs:-}/merged" && umount -Rf "${abs_overlayfs:-}/merged" || true
}

mount_runtime() {
  local abs_overlayfs="${1}"
  mkdir -p "${abs_overlayfs:-}/merged/etc" #|| die "Creating runtime directories failed: ${abs_overlayfs:-}/merged/etc"
  cp -pf /etc/{passwd,group,shadow} "${abs_overlayfs:-}/merged/etc/" || die "Copying /etc/passwd, /etc/group, and /etc/shadow failed: ${abs_overlayfs:-}/merged/etc/"
  # setfacl --remove-all "${abs_overlayfs:-}"/merged/etc/{passwd,group,shadow} || die "Removing ACLs from /etc/passwd, /etc/group, and /etc/shadow failed: ${abs_overlayfs:-}/merged/etc/"

  mountpoint -q "${abs_overlayfs:-}/merged/dev" || mount --bind /dev "${abs_overlayfs:-}/merged/dev" || die "Mounting /dev failed: ${abs_overlayfs:-}/merged/dev"
  mountpoint -q "${abs_overlayfs:-}/merged/dev/pts" || mount --bind /dev/pts "${abs_overlayfs:-}/merged/dev/pts" || die "Mounting /dev/pts failed: ${abs_overlayfs:-}/merged/dev/pts"
  mountpoint -q "${abs_overlayfs:-}/merged/proc" || mount --bind /proc "${abs_overlayfs:-}/merged/proc" || die "Mounting /proc failed: ${abs_overlayfs:-}/merged/proc"
  mountpoint -q "${abs_overlayfs:-}/merged/sys" || mount --bind /sys "${abs_overlayfs:-}/merged/sys" || die "Mounting /sys failed: ${abs_overlayfs:-}/merged/sys"

  mkdir -p "${abs_overlayfs:-}"/merged/run/{resolvconf,udev} #|| die "Creating runtime directories failed: ${abs_overlayfs:-}/merged/run"
  mountpoint -q "${abs_overlayfs:-}/merged/run/resolvconf" || mount --bind /run/resolvconf "${abs_overlayfs:-}/merged/run/resolvconf" || die "Mounting /run/resolvconf failed: ${abs_overlayfs:-}/merged/run/resolvconf"
  mountpoint -q "${abs_overlayfs:-}/merged/run/udev" || mount --bind /run/udev "${abs_overlayfs:-}/merged/run/udev" || die "Mounting /run/udev failed: ${abs_overlayfs:-}/merged/run/udev"
}

unmount_runtime() {
  local abs_overlayfs="${1}"
  mountpoint -q "${abs_overlayfs:-}/merged/dev/pts" && umount "${abs_overlayfs:-}/merged/dev/pts" || true
  mountpoint -q "${abs_overlayfs:-}/merged/dev" && umount "${abs_overlayfs:-}/merged/dev" || true
  mountpoint -q "${abs_overlayfs:-}/merged/proc" && umount "${abs_overlayfs:-}/merged/proc" || true
  mountpoint -q "${abs_overlayfs:-}/merged/sys" && umount "${abs_overlayfs:-}/merged/sys" || true
  mountpoint -q "${abs_overlayfs:-}/merged/run/resolvconf" && umount "${abs_overlayfs:-}/merged/run/resolvconf" || true
  mountpoint -q "${abs_overlayfs:-}/merged/run/udev" && umount "${abs_overlayfs:-}/merged/run/udev" || true
}

do_status() {
  local abs_overlayfs
  abs_overlayfs="$(resolve_overlayfs)"

  mountpoint -q "${abs_overlayfs:-}/merged" || return 1
  for p in /proc/*/root; do readlink -f "$p" 2>/dev/null | grep -q "^$(realpath "${abs_overlayfs}")/merged" && return 0; done
  return 3
}

do_start() {
  local abs_overlayfs single_port mirror_port novnc_passwd
  abs_overlayfs="$(resolve_overlayfs)"
  while [[ $# -gt 0 ]]; do
    case "${1}" in
      -s | --single-port)
        single_port="${2}"
        shift 2
        ;;
      -s=* | --single-port=*)
        single_port="${1#*=}"
        shift
        ;;
      -m | --mirror-port)
        mirror_port="${2}"
        shift 2
        ;;
      -m=* | --mirror-port=*)
        mirror_port="${1#*=}"
        shift
        ;;
      -p | --novnc-passwd)
        novnc_passwd="${2}"
        shift 2
        ;;
      -p=* | --novnc-passwd=*)
        novnc_passwd="${1#*=}"
        shift
        ;;
      *)
        die "Unknown option: ${1}"
        ;;
    esac
  done
  # Mount the chroot environment
  mount_overlayfs "${abs_overlayfs}"
  mount_runtime "${abs_overlayfs}"

  # Start the desktop environment and VNC server in the chroot environment.
  chroot "${abs_overlayfs}/merged" env TERM=xterm-256color DEBIAN_FRONTEND=noninteractive bash <<EOF
chvt 7
[ -z "$(getent group lightdm)" ] && groupadd lightdm
[ -z "$(getent passwd lightdm)" ] && useradd -d /var/lib/lightdm -s /bin/false -g lightdm lightdm
mkdir -p /var/lib/lightdm
chown lightdm:lightdm /var/lib/lightdm
chmod 700 /var/lib/lightdm
sleep 1
pkill -f Xorg 2>/dev/null || true
sleep 1
/etc/init.d/dbus restart
sleep 1
/etc/init.d/x11-common restart
sleep 1
/etc/init.d/lightdm restart
sleep 1
/etc/init.d/pulseaudio-enable-autospawn restart
sleep 1
/usr/sbin/NetworkManager --no-daemon &
sleep 1
mkdir -p /root/.vnc
printf "${novnc_passwd:-passwd}\n${novnc_passwd:-passwd}\nn\n\n" | vncpasswd /root/.vnc/passwd 2>/dev/null
sleep 1
kill \$(ps aux | grep 'novnc_proxy --vnc 0.0.0.0:5900' | grep -v grep | awk '{print \$2}') 2>/dev/null || true
/usr/share/novnc/utils/novnc_proxy --vnc 0.0.0.0:5900 --listen 0.0.0.0:${single_port:-6080} >/var/log/novnc_proxy_${single_port:-6080}.log 2>&1 &
sleep 1
pkill -f x11vnc 2>/dev/null || true
while true; do x11vnc -auth guess -display :0 -rfbauth /root/.vnc/passwd -rfbport 5901 -forever -shared >/var/log/x11vnc.log 2>&1 || true; done &
kill \$(ps aux | grep 'novnc_proxy --vnc 0.0.0.0:5901' | grep -v grep | awk '{print \$2}') 2>/dev/null || true
/usr/share/novnc/utils/novnc_proxy --vnc 0.0.0.0:5901 --listen 0.0.0.0:${mirror_port:-6081} >/var/log/novnc_proxy_${mirror_port:-6081}.log 2>&1 &
EOF
  [ $? -ne 0 ] && die "Starting desktop environment failed: ${abs_overlayfs:-}/merged"
  return 0
}

do_stop() {
  local abs_overlayfs
  abs_overlayfs="$(resolve_overlayfs)"

  do_status || die "Chroot environment is not running."

  # Change to virtual terminal 1 to stop the desktop environment
  chroot "${abs_overlayfs}/merged" env TERM=xterm-256color DEBIAN_FRONTEND=noninteractive bash -c "chvt 1" || true

  # Kill all processes running in the chroot environment to ensure they don't keep running after unmounting.
  for p in /proc/*/root; do readlink -f "$p" 2>/dev/null | grep -q "^$(realpath "${abs_overlayfs}")/merged" && kill -9 "${p//[^0-9]/}" 2>/dev/null; done

  # Unmount the chroot environment
  unmount_runtime "${abs_overlayfs}"
  unmount_overlayfs "${abs_overlayfs}"
}

do_run() {
  local rc status abs_overlayfs
  abs_overlayfs="$(resolve_overlayfs)"

  status="$(do_status && echo true || echo false)"
  if [ "${status}" != "true" ]; then
    mount_overlayfs "${abs_overlayfs}"
    mount_runtime "${abs_overlayfs}"
  fi
  log "Executing command: rootfs=${abs_overlayfs}, cmd=$*"
  chroot "${abs_overlayfs}/merged" env TERM=xterm-256color DEBIAN_FRONTEND=noninteractive bash -lc "$*" 2>&1
  rc=$?
  if [ "${status}" != "true" ]; then
    unmount_runtime "${abs_overlayfs}"
    unmount_overlayfs "${abs_overlayfs}"
  fi
  return "${rc}"
}

do_install() {
  local abs_overlayfs install_recommends install_chinese install_commonly apt_flag
  abs_overlayfs="$(resolve_overlayfs)"
  while [[ $# -gt 0 ]]; do
    case "${1}" in
      -r | --install-recommends)
        install_recommends="${2}"
        shift 2
        ;;
      -r=* | --install-recommends=*)
        install_recommends="${1#*=}"
        shift
        ;;
      -z | --install-chinese)
        install_chinese="${2}"
        shift 2
        ;;
      -z=* | --install-chinese=*)
        install_chinese="${1#*=}"
        shift
        ;;
      -c | --install-commonly)
        install_commonly="${2}"
        shift 2
        ;;
      -c=* | --install-commonly=*)
        install_commonly="${1#*=}"
        shift
        ;;
      *)
        die "Unknown option: ${1}"
        ;;
    esac
  done

  # Mount the chroot environment
  mount_overlayfs "${abs_overlayfs}"
  mount_runtime "${abs_overlayfs}"

  [ "${install_recommends:-yes}" = "yes" ] && apt_flag="" || apt_flag="--no-install-recommends"
  # Install packages and do necessary configuration in the chroot environment
  # Install XFCE desktop environment and Xvfb.
  chroot "${abs_overlayfs:-}/merged" env TERM=xterm-256color DEBIAN_FRONTEND=noninteractive bash -c "apt-get update && apt-get install -y ${apt_flag:-} dbus-x11 task-xfce-desktop xvfb" || die "Installing XFCE failed: ${abs_overlayfs:-}/merged"
  # Install libpam-mkhomedir and configure PAM to automatically create home directories for new users.
  chroot "${abs_overlayfs:-}/merged" env TERM=xterm-256color DEBIAN_FRONTEND=noninteractive bash -c "apt-get update && apt-get install -y ${apt_flag:-} libpam-mkhomedir" || die "Installing libpam-mkhomedir failed: ${abs_overlayfs:-}/merged"
  if ! grep -q "pam_mkhomedir.so" "${abs_overlayfs:-}/merged/etc/pam.d/common-session"; then
    printf "session required pam_mkhomedir.so skel=/etc/skel umask=0022\n" >>"${abs_overlayfs:-}/merged/etc/pam.d/common-session"
  fi
  # Install TigerVNC server and noVNC for VNC access, and set a default VNC password.
  chroot "${abs_overlayfs:-}/merged" env TERM=xterm-256color DEBIAN_FRONTEND=noninteractive bash -c "apt-get update && apt-get install -y ${apt_flag:-} tigervnc-standalone-server tigervnc-tools x11vnc novnc websockify" || die "Installing TigerVNC server and noVNC failed: ${abs_overlayfs:-}/merged"
  chroot "${abs_overlayfs:-}/merged" env TERM=xterm-256color DEBIAN_FRONTEND=noninteractive bash -c "mkdir -p /root/.vnc && printf 'passwd\npasswd\nn\n\n' | vncpasswd /root/.vnc/passwd 2>/dev/null" || die "Setting VNC password failed: ${abs_overlayfs:-}/merged"
  # Configure LightDM to start a VNC server for remote desktop access.
  chroot "${abs_overlayfs:-}/merged" env TERM=xterm-256color DEBIAN_FRONTEND=noninteractive bash <<EOF
apt-get update && apt-get install -y ${apt_flag:-} crudini
crudini --del /etc/lightdm/lightdm.conf VNCServer
crudini --set /etc/lightdm/lightdm.conf VNCServer enabled true
crudini --set /etc/lightdm/lightdm.conf VNCServer command 'Xvnc -rfbauth /root/.vnc/passwd'
crudini --set /etc/lightdm/lightdm.conf VNCServer port 5900
crudini --set /etc/lightdm/lightdm.conf VNCServer listen-address 0.0.0.0
crudini --set /etc/lightdm/lightdm.conf VNCServer width 1920
crudini --set /etc/lightdm/lightdm.conf VNCServer height 1080
crudini --set /etc/lightdm/lightdm.conf VNCServer depth 24
EOF
  [ $? -ne 0 ] && die "Configuring LightDM failed: ${abs_overlayfs:-}/merged"
  # Create a script to add the user to necessary groups when they log in, and configure LightDM to execute this script at session setup.
  mkdir -p "${abs_overlayfs:-}/merged/etc/lightdm" "${abs_overlayfs:-}/merged/etc/lightdm/lightdm.conf.d"
  printf '[Seat:*]\nsession-setup-script=/etc/lightdm/usermod-user.sh\n' >"${abs_overlayfs:-}/merged/etc/lightdm/lightdm.conf.d/50-usermod-user.conf"
  printf '#!/bin/env bash\n[ -n "$USER" ] && usermod -aG sudo,audio,video,input "$USER"\n[ -n "$USER" ] && usermod -s /bin/bash "$USER"\nexit 0\n' >"${abs_overlayfs:-}/merged/etc/lightdm/usermod-user.sh"
  chmod a+x "${abs_overlayfs:-}/merged/etc/lightdm/usermod-user.sh"
  if [ "${install_chinese:-yes}" = "yes" ]; then
    # Install additional packages and fonts for better user experience.
    chroot "${abs_overlayfs:-}/merged" env TERM=xterm-256color DEBIAN_FRONTEND=noninteractive bash <<EOF
apt-get update && apt-get install -y ${apt_flag:-} tzdata locales fonts-noto-cjk fonts-arphic-uming fonts-wqy-zenhei fonts-wqy-microhei
sed -i 's/^#.*zh_CN.UTF-8/zh_CN.UTF-8/g' /etc/locale.gen
locale-gen zh_CN.UTF-8
update-locale LANG=zh_CN.UTF-8
EOF
    [ $? -ne 0 ] && die "Installing additional packages and configuring locale failed: ${abs_overlayfs:-}/merged"
  fi
  #   if [ "${install_commonly:-yes}" = "yes" ]; then
  #     # Install additional packages and fonts for better user experience.
  #     chroot "${abs_overlayfs:-}/merged" env TERM=xterm-256color DEBIAN_FRONTEND=noninteractive bash <<EOF
  # mkdir -p /tmp
  # curl -skL https://dldir1v6.qq.com/weixin/Universal/Linux/WeChatLinux_x86_64.deb -o /tmp/WeChatLinux_x86_64.deb
  # dpkg -i /tmp/WeChatLinux_x86_64.deb || { apt-get update; apt-get install -f; dpkg -i /tmp/WeChatLinux_x86_64.deb || true; }
  # EOF
  #     [ $? -ne 0 ] && die "Installing additional packages and configuring locale failed: ${abs_overlayfs:-}/merged"
  #   fi
  # Kill all processes running in the chroot environment to ensure they don't keep running after unmounting.
  for p in /proc/*/root; do readlink -f "$p" 2>/dev/null | grep -q "^$(realpath "${abs_overlayfs}")/merged" && kill -9 "${p//[^0-9]/}" 2>/dev/null; done
  # Unmount the chroot environment
  unmount_runtime "${abs_overlayfs}"
  unmount_overlayfs "${abs_overlayfs}"
}

do_uninstall() {
  local abs_overlayfs
  abs_overlayfs="$(resolve_overlayfs)"

  do_status && chroot "${abs_overlayfs}/merged" env TERM=xterm-256color DEBIAN_FRONTEND=noninteractive bash -c "chvt 1" || true

  # Kill all processes running in the chroot environment to ensure they don't keep running after unmounting.
  for p in /proc/*/root; do readlink -f "$p" 2>/dev/null | grep -q "^$(realpath "${abs_overlayfs}")/merged" && kill -9 "${p//[^0-9]/}" 2>/dev/null; done

  # Unmount the chroot environment
  unmount_runtime "${abs_overlayfs}"
  unmount_overlayfs "${abs_overlayfs}"

  rm -rf "${abs_overlayfs:-}"
}

main() {
  local action="${1:-}"
  [ -n "${action}" ] || {
    usage
    exit 1
  }
  shift || true

  case "${action}" in
    -i | --install) do_install "$@" ;;
    -u | --uninstall) do_uninstall "$@" ;;
    -s | --start) do_start "$@" ;;
    -q | --stop) do_stop "$@" ;;
    -t | --status) do_status "$@" ;;
    -r | --run) do_run "$@" ;;
    -h | --help) usage ;;
    *) die "Unknown command: ${action}" ;;
  esac
}

[ "${EUID}" -eq 0 ] || die "Please run as root."

main "$@"
