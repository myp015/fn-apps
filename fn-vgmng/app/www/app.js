const CGI_BASE_PATH = location.pathname.includes('index.cgi') ? '../www/cgi-bin/' : './cgi-bin/';
const STATUS_ENDPOINT = `${CGI_BASE_PATH}status.cgi`;
const ACTION_ENDPOINT = `${CGI_BASE_PATH}action.cgi`;
const SUPPORTED_FILESYSTEMS = new Set(['btrfs', 'ext4', 'ext3', 'ext2', 'xfs', 'ntfs', 'ntfs3', 'exfat', 'vfat']);
const RECOGNIZED_UNMOUNTABLE_FILESYSTEMS = new Set(['swap']);

const appState = {
  statusData: null,
};

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[char]));
}

function displayText(value, fallback = '-') {
  return value && value !== '' ? value : fallback;
}

function filesystemName(device) {
  return String(device?.fstype || '').toLowerCase();
}

function showToast(message, isError = false) {
  const toastNode = document.getElementById('toast');
  if (!toastNode) {
    return;
  }

  toastNode.textContent = message;
  toastNode.style.background = isError ? 'rgba(180, 35, 24, 0.92)' : 'rgba(24, 37, 29, 0.92)';
  toastNode.classList.remove('hidden');
  clearTimeout(toastNode._timer);
  toastNode._timer = setTimeout(() => toastNode.classList.add('hidden'), 3200);
}

function flattenDevices(devices, flattened = [], parentPath = null) {
  for (const device of devices || []) {
    flattened.push({
      ...device,
      parent: parentPath,
      children: undefined,
    });

    if (device.children && device.children.length) {
      flattenDevices(device.children, flattened, device.name || device.path || null);
    }
  }

  return flattened;
}

function isImportableDevice(device) {
  if (!device?.path || !SUPPORTED_FILESYSTEMS.has(filesystemName(device))) {
    return false;
  }
  if (device.mountpoint === '/' || device.mountpoint === '/boot/efi') {
    return false;
  }
  return true;
}

function normalizePath(path = '') {
  const normalized = String(path || '');
  if (normalized === '/') {
    return '/';
  }
  return normalized.replace(/\/+$/, '');
}

function normalizeMountSource(source = '') {
  return String(source || '').replace(/\[[^\]]*\]$/, '');
}

function pathBase(path = '') {
  const normalized = String(path || '').replace(/\/+$/, '');
  if (!normalized) {
    return '';
  }
  const segments = normalized.split('/').filter(Boolean);
  return segments.length ? segments[segments.length - 1] : normalized;
}

function cleanLabel(label = '') {
  const normalized = String(label || '').trim();
  if (!normalized || /\\x[0-9a-f]{2}/i.test(normalized)) {
    return '';
  }
  return normalized;
}

function mountModeFromOptions(options = '') {
  return String(options || '').includes('rw') ? 'rw' : 'ro';
}

function mountModeText(mode = '') {
  return mode === 'rw' ? '读写' : '只读';
}

function autoMountCheckboxId(path = '') {
  return `auto-mount-${String(path || '').replace(/[^a-zA-Z0-9_-]/g, '-')}`;
}

function getManagedRoots(statusData = {}) {
  return [normalizePath(statusData?.mountRoot || ''), normalizePath(statusData?.mountAliasRoot || '')].filter(Boolean);
}

function isManagedMount(target = '', managedRoots = []) {
  const normalizedTarget = normalizePath(target);
  if (!normalizedTarget || !managedRoots.length) {
    return false;
  }
  return managedRoots.some((rootPath) => normalizedTarget === rootPath || normalizedTarget.startsWith(`${rootPath}/`));
}

function displayMountTarget(target = '', statusData = {}) {
  const normalizedTarget = normalizePath(target);
  const mountRoot = normalizePath(statusData?.mountRoot || '');
  const aliasRoot = normalizePath(statusData?.mountAliasRoot || '');

  if (!normalizedTarget || !mountRoot || !aliasRoot) {
    return normalizedTarget;
  }
  if (normalizedTarget === mountRoot) {
    return aliasRoot;
  }
  if (normalizedTarget.startsWith(`${mountRoot}/`)) {
    return `${aliasRoot}${normalizedTarget.slice(mountRoot.length)}`;
  }
  return normalizedTarget;
}

function deviceTitle(path = '', label = '', fallback = '') {
  return cleanLabel(label) || pathBase(path) || fallback || '-';
}

function devicePathCell(path = '', fallback = '') {
  const title = pathBase(path) || fallback || '-';
  const normalizedPath = displayText(path, '');

  if (!normalizedPath || title === normalizedPath) {
    return `<span class="mono">${escapeHtml(normalizedPath || title)}</span>`;
  }

  return `
    <div class="identity-title">${escapeHtml(title)}</div>
    <div class="identity-path mono">${escapeHtml(normalizedPath)}</div>
  `;
}

function upsertMountEntry(mountStore, mountEntry, managedRoots) {
  const mountTarget = normalizePath(mountEntry?.target || '');
  if (!isManagedMount(mountTarget, managedRoots)) {
    return;
  }

  const mountSource = normalizeMountSource(mountEntry?.source || mountEntry?.path || '');
  const mountKey = `${mountTarget}::${mountSource}`;
  const previousEntry = mountStore.get(mountKey) || {};

  mountStore.set(mountKey, {
    target: mountTarget,
    source: mountSource || previousEntry.source || '',
    fstype: mountEntry?.fstype || previousEntry.fstype || '',
    options: mountEntry?.options || previousEntry.options || '',
  });
}

function collectMountedEntries(statusData) {
  const managedMounts = new Map();
  const lsblkDevices = flattenDevices(statusData?.lsblk?.blockdevices || []);
  const inventoryDisks = statusData?.inventory?.disks || [];
  const managedRoots = getManagedRoots(statusData);

  (statusData?.findmnt?.filesystems || []).forEach((filesystem) => {
    upsertMountEntry(managedMounts, filesystem, managedRoots);
  });

  lsblkDevices.forEach((device) => {
    if (device?.mountpoint) {
      upsertMountEntry(managedMounts, {
        target: device.mountpoint,
        source: device.path,
        fstype: device.fstype,
      }, managedRoots);
    }
  });

  inventoryDisks.forEach((disk) => {
    (disk.partitions || []).forEach((partition) => {
      if (partition?.mountpoint) {
        upsertMountEntry(managedMounts, {
          target: partition.mountpoint,
          source: partition.path,
          fstype: partition.fstype,
        }, managedRoots);
      }
    });
  });

  return Array.from(managedMounts.values()).sort((left, right) => left.target.localeCompare(right.target));
}

function collectMountedSources(mountEntries) {
  return new Set(mountEntries.map((entry) => normalizeMountSource(entry.source)).filter(Boolean));
}

function isImportableInventoryEntry(entry) {
  if (!entry?.path || !SUPPORTED_FILESYSTEMS.has(filesystemName(entry))) {
    return false;
  }
  if (entry.mountpoint === '/' || entry.mountpoint === '/boot/efi') {
    return false;
  }
  return true;
}

function buildCandidateDevices(statusData, mountedEntries) {
  const mountedSources = collectMountedSources(mountedEntries);
  const candidateByPath = new Map();
  const flatDevices = flattenDevices(statusData?.lsblk?.blockdevices || []);

  flatDevices.forEach((device) => {
    const devicePath = normalizeMountSource(device?.path);
    if (!devicePath || !isImportableDevice(device) || device.mountpoint || mountedSources.has(devicePath)) {
      return;
    }

    candidateByPath.set(devicePath, {
      path: device.path,
      label: device.label || device.name || '',
      size: device.size || '',
      fstype: device.fstype || '',
      type: device.type || '',
    });
  });

  (statusData?.inventory?.disks || []).forEach((disk) => {
    const diskPath = normalizeMountSource(disk?.path);
    if (diskPath && isImportableInventoryEntry(disk) && !disk.mountpoint && !mountedSources.has(diskPath) && !candidateByPath.has(diskPath)) {
      candidateByPath.set(diskPath, {
        path: disk.path,
        label: disk.label || '',
        size: disk.size || '',
        fstype: disk.fstype || '',
        type: 'disk',
      });
    }

    (disk.partitions || []).forEach((partition) => {
      const partitionPath = normalizeMountSource(partition?.path);
      if (!partitionPath || !isImportableInventoryEntry(partition) || partition.mountpoint || mountedSources.has(partitionPath) || candidateByPath.has(partitionPath)) {
        return;
      }

      candidateByPath.set(partitionPath, {
        path: partition.path,
        label: partition.label || '',
        size: partition.size || '',
        fstype: partition.fstype || '',
        type: 'part',
      });
    });
  });

  return Array.from(candidateByPath.values()).sort((left, right) => left.path.localeCompare(right.path));
}

function collectAllMountTargets(statusData) {
  const mountTargetBySource = new Map();

  (statusData?.findmnt?.filesystems || []).forEach((filesystem) => {
    const source = normalizeMountSource(filesystem?.source || filesystem?.path || '');
    const target = filesystem?.target || '';
    if (source && target && !mountTargetBySource.has(source)) {
      mountTargetBySource.set(source, target);
    }
  });

  flattenDevices(statusData?.lsblk?.blockdevices || []).forEach((device) => {
    const source = normalizeMountSource(device?.path);
    const target = device?.mountpoint || '';
    if (source && target && !mountTargetBySource.has(source)) {
      mountTargetBySource.set(source, target);
    }
  });

  (statusData?.inventory?.disks || []).forEach((disk) => {
    (disk.partitions || []).forEach((partition) => {
      const source = normalizeMountSource(partition?.path);
      const target = partition?.mountpoint || '';
      if (source && target && !mountTargetBySource.has(source)) {
        mountTargetBySource.set(source, target);
      }
    });
  });

  return mountTargetBySource;
}

function countPartitions(disks) {
  return disks.reduce((count, disk) => count + (disk.partitions || []).length, 0);
}

function statCard(label, value, tone = '') {
  return `
    <div class="stat-card${tone ? ` ${tone}` : ''}">
      <div class="stat-value">${escapeHtml(value)}</div>
      <div class="stat-label">${escapeHtml(label)}</div>
    </div>
  `;
}

function renderOverview(_statusData, candidateDevices, mountedEntries, inventoryDisks) {
  const statsNode = document.getElementById('overviewStats');
  if (!statsNode) {
    return;
  }

  statsNode.innerHTML = [
    statCard('磁盘', String(inventoryDisks.length)),
    statCard('分区', String(countPartitions(inventoryDisks))),
    statCard('可挂载卷', String(candidateDevices.length), candidateDevices.length ? 'ok' : ''),
    statCard('已挂载卷', String(mountedEntries.length), mountedEntries.length ? 'info' : ''),
  ].join('');
}

function inventoryStatus(partition, mountedTarget = '') {
  const fstype = filesystemName(partition);
  if (partition.mountpoint || mountedTarget) {
    return '已挂载';
  }
  if (SUPPORTED_FILESYSTEMS.has(fstype)) {
    return '可挂载';
  }
  if (fstype === 'lvm2_member') {
    return 'LVM 物理卷';
  }
  if (fstype === 'linux_raid_member' || /raid/i.test(partition.partType || '')) {
    return 'RAID 成员';
  }
  return '-';
}

function summarizeDiskStatus(disk, candidateByPath, mountedBySource) {
  const partitions = disk.partitions || [];
  if (partitions.find((partition) => mountedBySource.has(normalizeMountSource(partition.path)))) {
    return '已有导入卷';
  }
  if (partitions.some((partition) => candidateByPath.has(partition.path))) {
    return '发现可挂载卷';
  }
  return '磁盘';
}

function getReportRows(statusData, key) {
  return statusData?.[key]?.report?.[0]?.[key.slice(0, -1)] || [];
}

function buildPartitionMaps(disks) {
  const partitionToDisk = new Map();
  const partitionMeta = new Map();

  disks.forEach((disk) => {
    (disk.partitions || []).forEach((partition) => {
      const normalizedPath = normalizeMountSource(partition.path);
      if (!normalizedPath) {
        return;
      }
      partitionToDisk.set(normalizedPath, disk.path);
      partitionMeta.set(normalizedPath, partition);
    });
  });

  return { partitionToDisk, partitionMeta };
}

function buildLsblkMaps(statusData) {
  const deviceByPath = new Map();
  const parentByPath = new Map();

  flattenDevices(statusData?.lsblk?.blockdevices || []).forEach((device) => {
    const normalizedPath = normalizeMountSource(device?.path);
    if (!normalizedPath) {
      return;
    }

    deviceByPath.set(normalizedPath, device);

    const pkname = device?.pkname
      ? (String(device.pkname).startsWith('/dev/') ? String(device.pkname) : `/dev/${device.pkname}`)
      : '';
    const parentPath = normalizeMountSource(device?.parent || pkname);
    if (parentPath) {
      parentByPath.set(normalizedPath, parentPath);
    }
  });

  return { deviceByPath, parentByPath };
}

function buildAutoMountMap(statusData) {
  const autoMountByDevice = new Map();

  (statusData?.autoMounts || []).forEach((entry) => {
    const devicePath = normalizeMountSource(entry?.device);
    if (devicePath) {
      autoMountByDevice.set(devicePath, entry);
    }
  });

  return autoMountByDevice;
}

function resolvePhysicalPartition(devicePath, resolutionContext, visited = new Set()) {
  const normalizedPath = normalizeMountSource(devicePath);
  if (!normalizedPath || visited.has(normalizedPath)) {
    return [];
  }

  visited.add(normalizedPath);

  if (resolutionContext.partitionToDisk.has(normalizedPath)) {
    return [normalizedPath];
  }

  const resolvedPartitions = new Set();
  const parentPath = resolutionContext.parentByPath.get(normalizedPath);

  if (parentPath) {
    resolvePhysicalPartition(parentPath, resolutionContext, visited).forEach((path) => resolvedPartitions.add(path));
  }

  return Array.from(resolvedPartitions);
}

function resolveVolumePartitions(devicePath, resolutionContext) {
  const normalizedPath = normalizeMountSource(devicePath);
  if (!normalizedPath) {
    return [];
  }

  const directPartitions = resolvePhysicalPartition(normalizedPath, resolutionContext);
  if (directPartitions.length) {
    return directPartitions;
  }

  const logicalVolume = resolutionContext.lvByPath.get(normalizedPath);
  if (!logicalVolume) {
    return [];
  }

  const resolvedPartitions = new Set();
  const physicalVolumes = resolutionContext.vgToPvs.get(logicalVolume.vg_name) || [];

  physicalVolumes.forEach((pvName) => {
    resolvePhysicalPartition(pvName, resolutionContext).forEach((path) => resolvedPartitions.add(path));
  });

  return Array.from(resolvedPartitions);
}

function buildVolumeEntries(statusData, disks, candidateDevices, mountedEntries) {
  const { partitionToDisk, partitionMeta } = buildPartitionMaps(disks);
  const { deviceByPath, parentByPath } = buildLsblkMaps(statusData);
  const physicalVolumes = getReportRows(statusData, 'pvs');
  const logicalVolumes = getReportRows(statusData, 'lvs');
  const vgToPvs = new Map();
  const lvByPath = new Map();

  physicalVolumes.forEach((physicalVolume) => {
    const vgName = physicalVolume?.vg_name;
    const pvPath = normalizeMountSource(physicalVolume?.pv_name);
    if (!vgName || !pvPath) {
      return;
    }
    const existing = vgToPvs.get(vgName) || [];
    existing.push(pvPath);
    vgToPvs.set(vgName, existing);
  });

  logicalVolumes.forEach((logicalVolume) => {
    const lvPath = normalizeMountSource(logicalVolume?.lv_path);
    if (lvPath) {
      lvByPath.set(lvPath, logicalVolume);
    }
  });

  const resolutionContext = {
    partitionToDisk,
    partitionMeta,
    vgToPvs,
    lvByPath,
    deviceByPath,
    parentByPath,
  };

  const volumeEntryByKey = new Map();

  mountedEntries.forEach((mountEntry) => {
    const volumeKey = normalizeMountSource(mountEntry.source) || normalizePath(mountEntry.target);
    if (!volumeKey) {
      return;
    }

    const lsblkDevice = deviceByPath.get(normalizeMountSource(mountEntry.source));
    const volumeEntry = volumeEntryByKey.get(volumeKey) || {
      path: normalizeMountSource(mountEntry.source) || '',
      label: '',
      size: '',
      fstype: mountEntry.fstype || '',
      mountTarget: '',
      mountOptions: '',
      mountMode: '',
      mounted: false,
      candidate: false,
    };

    volumeEntry.path = volumeEntry.path || normalizeMountSource(mountEntry.source) || '';
    volumeEntry.label = volumeEntry.label || lsblkDevice?.label || lsblkDevice?.name || '';
    volumeEntry.size = volumeEntry.size || lsblkDevice?.size || '';
    volumeEntry.fstype = volumeEntry.fstype || mountEntry.fstype || '';
    volumeEntry.mountTarget = mountEntry.target || lsblkDevice?.mountpoint || volumeEntry.mountTarget;
    volumeEntry.mountOptions = mountEntry.options || volumeEntry.mountOptions;
    volumeEntry.mountMode = mountModeFromOptions(mountEntry.options || volumeEntry.mountOptions);
    volumeEntry.mounted = true;
    volumeEntryByKey.set(volumeKey, volumeEntry);
  });

  candidateDevices.forEach((candidateDevice) => {
    const volumeKey = normalizeMountSource(candidateDevice.path);
    if (!volumeKey) {
      return;
    }

    const lsblkDevice = deviceByPath.get(volumeKey);
    const volumeEntry = volumeEntryByKey.get(volumeKey) || {
      path: candidateDevice.path || '',
      label: '',
      size: '',
      fstype: '',
      mountTarget: '',
      mountOptions: '',
      mountMode: '',
      mounted: false,
      candidate: false,
    };

    volumeEntry.path = volumeEntry.path || candidateDevice.path || '';
    volumeEntry.label = volumeEntry.label || candidateDevice.label || lsblkDevice?.label || lsblkDevice?.name || '';
    volumeEntry.size = volumeEntry.size || candidateDevice.size || lsblkDevice?.size || '';
    volumeEntry.fstype = volumeEntry.fstype || candidateDevice.fstype || '';
    volumeEntry.candidate = true;
    volumeEntryByKey.set(volumeKey, volumeEntry);
  });

  deviceByPath.forEach((device, devicePath) => {
    const normalizedPath = normalizeMountSource(devicePath);
    const deviceFstype = filesystemName(device);
    if (!normalizedPath || volumeEntryByKey.has(normalizedPath) || !RECOGNIZED_UNMOUNTABLE_FILESYSTEMS.has(deviceFstype)) {
      return;
    }

    volumeEntryByKey.set(normalizedPath, {
      path: device.path || normalizedPath,
      label: device.label || device.name || '',
      size: device.size || '',
      fstype: device.fstype || '',
      mountTarget: '',
      mountOptions: '',
      mountMode: '',
      mounted: false,
      candidate: false,
    });
  });

  return Array.from(volumeEntryByKey.values())
    .map((volumeEntry) => {
      const normalizedPath = normalizeMountSource(volumeEntry.path);
      const lsblkDevice = deviceByPath.get(normalizedPath);
      const logicalVolume = lvByPath.get(normalizedPath);
      const relatedPartitions = resolveVolumePartitions(volumeEntry.path, resolutionContext);
      const relatedDisks = Array.from(new Set(
        relatedPartitions.map((partitionPath) => resolutionContext.partitionToDisk.get(partitionPath)).filter(Boolean),
      ));

      return {
        ...volumeEntry,
        label: volumeEntry.label || lsblkDevice?.label || logicalVolume?.lv_name || lsblkDevice?.name || '',
        size: volumeEntry.size || lsblkDevice?.size || logicalVolume?.lv_size || '',
        fstype: volumeEntry.fstype || lsblkDevice?.fstype || '',
        mountTarget: volumeEntry.mountTarget || lsblkDevice?.mountpoint || '',
        mountOptions: volumeEntry.mountOptions || '',
        mountMode: volumeEntry.mountMode || mountModeFromOptions(volumeEntry.mountOptions),
        mountmode: relatedPartitions.some((partitionPath) => resolutionContext.partitionMeta.get(partitionPath)?.mountmode === 'ro') ? 'ro' : 'rw',
        partitions: relatedPartitions,
        disks: relatedDisks,
        relation: relatedPartitions
          .map((partitionPath) => {
            const partition = resolutionContext.partitionMeta.get(partitionPath);
            const partLabel = deviceTitle(partitionPath, partition?.label, partition?.partType || '分区');
            return `${partLabel} @ ${partitionPath}`;
          })
          .join(' @ '),
      };
    })
    .sort((left, right) => left.path.localeCompare(right.path));
}

function collectPartitionMountTargets(volumes) {
  const mountTargetByPartition = new Map();

  (volumes || []).forEach((volume) => {
    const mountTarget = normalizePath(volume?.mountTarget || '');
    if (!mountTarget) {
      return;
    }

    (volume.partitions || []).forEach((partitionPath) => {
      const normalizedPartition = normalizeMountSource(partitionPath);
      if (normalizedPartition && !mountTargetByPartition.has(normalizedPartition)) {
        mountTargetByPartition.set(normalizedPartition, mountTarget);
      }
    });
  });

  return mountTargetByPartition;
}

function renderInventory(disks, candidateDevices, mountedEntries, statusData, volumes) {
  const tableContainer = document.getElementById('inventoryTableWrap');
  const emptyState = document.getElementById('inventoryEmpty');
  const metaNode = document.getElementById('inventoryMeta');

  if (!tableContainer || !emptyState || !metaNode) {
    return;
  }

  const candidateByPath = new Map(candidateDevices.map((candidateDevice) => [candidateDevice.path, candidateDevice]));
  const mountedBySource = new Map(mountedEntries.map((mountEntry) => [normalizeMountSource(mountEntry.source), mountEntry]));
  const allMountTargets = collectAllMountTargets(statusData);
  const partitionMountTargets = collectPartitionMountTargets(volumes);
  const { deviceByPath } = buildLsblkMaps(statusData);

  if (!disks.length) {
    tableContainer.classList.add('hidden');
    emptyState.classList.remove('hidden');
    tableContainer.innerHTML = '';
    metaNode.textContent = '';
    return;
  }

  metaNode.textContent = `${disks.length} 块磁盘，${countPartitions(disks)} 个分区`;

  const diskRows = disks.map((disk) => {
    const partitions = disk.partitions || [];
    const rows = [];

    rows.push(`
      <tr class="table-row disk-row">
        <td class="device-cell">${devicePathCell(disk.path, '磁盘')}</td>
        <td>${escapeHtml(cleanLabel(disk.label) || '-')}</td>
        <td>-</td>
        <td>${escapeHtml(displayText(disk.size))}</td>
        <td>${escapeHtml(displayText(disk.fstype, '-'))}</td>
        <td>${escapeHtml(summarizeDiskStatus(disk, candidateByPath, mountedBySource))}</td>
        <td>-</td>
      </tr>
    `);

    if (!partitions.length) {
      rows.push(`
        <tr class="table-row partition-row muted-row">
          <td class="device-cell subdevice">无分区</td>
          <td>-</td>
          <td>-</td>
          <td>-</td>
          <td>-</td>
          <td>仅检测到整盘</td>
          <td>-</td>
        </tr>
      `);
      return rows.join('');
    }

    partitions.forEach((partition) => {
      const normalizedPartitionPath = normalizeMountSource(partition.path);
      const mountedEntry = mountedBySource.get(normalizedPartitionPath);
      const candidateEntry = candidateByPath.get(partition.path);
      const lsblkPartition = deviceByPath.get(normalizedPartitionPath);
      const mountedTarget = mountedEntry?.target
        || partitionMountTargets.get(normalizedPartitionPath)
        || allMountTargets.get(normalizedPartitionPath)
        || partition.mountpoint
        || lsblkPartition?.mountpoint
        || '';
      const statusLabel = mountedTarget
        ? '已挂载'
        : candidateEntry
          ? '可形成卷'
          : inventoryStatus(partition, mountedTarget);

      rows.push(`
        <tr class="table-row partition-row">
          <td class="device-cell subdevice">${devicePathCell(partition.path, partition.partType || '分区')}</td>
          <td>${escapeHtml(cleanLabel(partition.label) || '-')}</td>
          <td>${escapeHtml(displayText(partition.partId, '-'))}</td>
          <td>${escapeHtml(displayText(partition.size))}</td>
          <td>${escapeHtml(displayText(partition.fstype))}</td>
          <td>${escapeHtml(statusLabel)}</td>
          <td class="mount-cell">${escapeHtml(mountedTarget ? displayMountTarget(mountedTarget, statusData) : '-')}</td>
        </tr>
      `);
    });

    return rows.join('');
  }).join('');

  tableContainer.innerHTML = `
    <table class="inventory-table">
      <thead>
        <tr>
          <th>设备</th>
          <th>标签</th>
          <th>分区 ID</th>
          <th>容量</th>
          <th>文件系统</th>
          <th>状态</th>
          <th>挂载位置</th>
        </tr>
      </thead>
      <tbody>
        ${diskRows}
      </tbody>
    </table>
  `;

  emptyState.classList.add('hidden');
  tableContainer.classList.remove('hidden');
}

function renderVolumes(volumes, statusData) {
  const tableContainer = document.getElementById('volumeTableWrap');
  const emptyState = document.getElementById('volumeEmpty');
  const metaNode = document.getElementById('volumeMeta');

  if (!tableContainer || !emptyState || !metaNode) {
    return;
  }

  const autoMountByDevice = buildAutoMountMap(statusData);

  metaNode.textContent = volumes.length ? `${volumes.length} 个卷` : '';

  if (!volumes.length) {
    tableContainer.classList.add('hidden');
    tableContainer.innerHTML = '';
    emptyState.classList.remove('hidden');
    return;
  }

  tableContainer.innerHTML = `
    <table class="inventory-table volume-table">
      <thead>
        <tr>
          <th>自动挂载</th>
          <th>卷设备</th>
          <th>卷标</th>
          <th>关联分区</th>
          <th>容量</th>
          <th>文件系统</th>
          <th>状态</th>
          <th>挂载位置</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        ${volumes.map((volume) => {
          const autoMountEntry = autoMountByDevice.get(normalizeMountSource(volume.path));
          const supportedMountMode = volume.mountmode || 'rw';
          const statusLabel = volume.mounted
            ? `已挂载(${mountModeText(volume.mountMode)})`
            : volume.candidate
              ? '可挂载'
              : RECOGNIZED_UNMOUNTABLE_FILESYSTEMS.has(filesystemName(volume))
                ? `不可挂载(${displayText(volume.fstype)})`
                : '已识别';
          const mountTarget = volume.mounted && volume.mountTarget ? displayMountTarget(volume.mountTarget, statusData) : '-';
          const checkboxId = autoMountCheckboxId(volume.path);
          const actions = volume.mounted
            ? `<button class="btn danger small" data-unmount="${escapeHtml(volume.mountTarget)}">卸载</button>`
            : volume.candidate
              ? (supportedMountMode === 'ro'
                ? `<button class="btn secondary small" data-mount="${escapeHtml(volume.path)}" data-mode="ro">只读</button>`
                : `<button class="btn secondary small" data-mount="${escapeHtml(volume.path)}" data-mode="ro">只读</button>
                   <button class="btn warn small" data-mount="${escapeHtml(volume.path)}" data-mode="rw">读写</button>`)
              : '-';
          const autoCheckbox = volume.mounted || volume.candidate
            ? `
                <label class="auto-mount-checkbox" for="${escapeHtml(checkboxId)}">
                  <input
                    id="${escapeHtml(checkboxId)}"
                    type="checkbox"
                    data-auto-checkbox="1"
                    ${volume.mounted ? 'data-mounted-auto-toggle="1"' : ''}
                    data-device="${escapeHtml(volume.path)}"
                    data-target="${escapeHtml(volume.mountTarget || '')}"
                    data-mode="${escapeHtml(supportedMountMode === 'ro' ? 'ro' : (autoMountEntry?.mode || volume.mountMode || 'ro'))}"
                    ${autoMountEntry ? 'checked' : ''}
                  >
                </label>
              `
            : '<span class="auto-mount-placeholder">-</span>';

          return `
            <tr class="table-row ${volume.mounted ? 'mounted-row' : volume.candidate ? 'candidate-row' : ''}" data-volume-path="${escapeHtml(volume.path)}" data-mountmode="${escapeHtml(supportedMountMode)}">
              <td class="auto-mount-cell">${autoCheckbox}</td>
              <td class="device-cell">${devicePathCell(volume.path, '卷设备')}</td>
              <td>${escapeHtml(cleanLabel(volume.label) || displayText(volume.label))}</td>
              <td>${escapeHtml(volume.relation || '-')}</td>
              <td>${escapeHtml(displayText(volume.size))}</td>
              <td>${escapeHtml(displayText(volume.fstype))}</td>
              <td>${escapeHtml(statusLabel)}</td>
              <td class="mount-cell">${escapeHtml(mountTarget)}</td>
              <td class="table-actions">${actions}</td>
            </tr>
          `;
        }).join('')}
      </tbody>
    </table>
  `;

  emptyState.classList.add('hidden');
  tableContainer.classList.remove('hidden');
}

async function loadStatus() {
  const response = await fetch(STATUS_ENDPOINT, {
    method: 'GET',
    cache: 'no-store',
  });

  if (!response.ok) {
    throw new Error(`获取状态失败（HTTP ${response.status}）`);
  }

  const statusData = await response.json();
  appState.statusData = statusData || {};
  renderApp();
  return appState.statusData;
}

async function invokeAction(action, params = {}) {
  const requestBody = new URLSearchParams({ action, ...params });
  const response = await fetch(ACTION_ENDPOINT, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
    },
    body: requestBody.toString(),
  });

  if (!response.ok) {
    throw new Error(`请求失败（HTTP ${response.status}）`);
  }

  const result = await response.json();
  if (!result?.ok) {
    throw new Error(result?.message || '操作失败');
  }

  if (result.status) {
    appState.statusData = result.status;
    renderApp();
  }

  return result;
}

function successToastMessage(action, params = {}) {
  switch (action) {
    case 'activate':
      return '扫描并组装完成';
    case 'mount':
      return params.mode === 'rw' ? '卷已按读写方式挂载' : '卷已按只读方式挂载';
    case 'unmount':
      return '卷已卸载';
    case 'auto-mount':
      return params.auto === '0' ? '已关闭自动挂载' : '已开启自动挂载';
    default:
      return '操作完成';
  }
}

async function applyVolumeAction(action, params = {}, successMessage = '') {
  try {
    await invokeAction(action, params);
    showToast(successMessage || successToastMessage(action, params), false);
  } catch (error) {
    showToast(error.message || '操作失败', true);
    throw error;
  }
}

async function refreshStatus({ activate = false, silent = false } = {}) {
  if (activate) {
    await invokeAction('activate');
    if (!silent) {
      showToast(successToastMessage('activate'), false);
    }
    return appState.statusData;
  }

  const statusData = await loadStatus();
  if (!silent) {
    showToast('状态已刷新', false);
  }
  return statusData;
}

function renderApp() {
  const statusData = appState.statusData || {};
  const mountedEntries = collectMountedEntries(statusData);
  const candidateDevices = buildCandidateDevices(statusData, mountedEntries);
  const inventoryDisks = statusData.inventory?.disks || [];
  const volumeEntries = buildVolumeEntries(statusData, inventoryDisks, candidateDevices, mountedEntries);

  renderOverview(statusData, candidateDevices, mountedEntries, inventoryDisks);
  renderInventory(inventoryDisks, candidateDevices, mountedEntries, statusData, volumeEntries);
  renderVolumes(volumeEntries, statusData);
}

function bindUiActions() {
  document.getElementById('assembleBtn')?.addEventListener('click', async () => {
    try {
      await refreshStatus({ activate: true });
    } catch (error) {
      showToast(error.message, true);
    }
  });

  document.getElementById('volumeTableWrap')?.addEventListener('click', async (event) => {
    const mountButton = event.target.closest('[data-mount]');
    const unmountButton = event.target.closest('[data-unmount]');

    if (mountButton) {
      const volumeRow = mountButton.closest('[data-volume-path]');
      const autoCheckbox = volumeRow?.querySelector('[data-auto-checkbox="1"]');
      const requestDevice = mountButton.getAttribute('data-mount') || '';
      const supportedMountMode = volumeRow?.getAttribute('data-mountmode') || 'rw';
      const requestMode = supportedMountMode === 'ro' ? 'ro' : (mountButton.getAttribute('data-mode') || 'ro');
      const requestAuto = autoCheckbox?.checked ? '1' : '0';

      if (!requestDevice) {
        return;
      }

      mountButton.disabled = true;
      try {
        await applyVolumeAction('mount', {
          device: requestDevice,
          mode: requestMode,
          auto: requestAuto,
        });
      } finally {
        mountButton.disabled = false;
      }
      return;
    }

    if (unmountButton) {
      const requestTarget = unmountButton.getAttribute('data-unmount') || '';
      if (!requestTarget) {
        return;
      }

      unmountButton.disabled = true;
      try {
        await applyVolumeAction('unmount', { target: requestTarget });
      } finally {
        unmountButton.disabled = false;
      }
    }
  });

  document.getElementById('volumeTableWrap')?.addEventListener('change', async (event) => {
    const autoCheckbox = event.target.closest('[data-auto-checkbox="1"]');
    if (!autoCheckbox) {
      return;
    }

    const requestDevice = autoCheckbox.getAttribute('data-device') || '';
    const requestTarget = autoCheckbox.getAttribute('data-target') || '';
    const requestMode = autoCheckbox.getAttribute('data-mode') || 'ro';
    const requestAuto = autoCheckbox.checked ? '1' : '0';

    if (!requestDevice) {
      return;
    }

    autoCheckbox.disabled = true;
    try {
      await applyVolumeAction('auto-mount', {
        device: requestDevice,
        target: requestTarget,
        mode: requestMode,
        auto: requestAuto,
      });
    } catch (_error) {
      autoCheckbox.checked = !autoCheckbox.checked;
    } finally {
      autoCheckbox.disabled = false;
    }
  });
}

window.addEventListener('load', async () => {
  bindUiActions();

  try {
    await refreshStatus({ activate: true, silent: true });
  } catch (error) {
    try {
      await loadStatus();
    } catch (fetchError) {
      showToast(fetchError.message, true);
    }
  }
});
