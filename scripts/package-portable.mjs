import { access, cp, mkdir, readFile, readdir, rm } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const executable = join(root, 'src-tauri', 'target', 'release', 'download-manager.exe');
const extension = join(root, 'extension', 'dist');
const output = join(root, 'portable');

function importedDlls(bytes) {
  const u16 = (offset) => bytes.readUInt16LE(offset);
  const u32 = (offset) => bytes.readUInt32LE(offset);
  if (u16(0) !== 0x5a4d) throw new Error('Portable executable has no DOS header');
  const peOffset = u32(0x3c);
  if (u32(peOffset) !== 0x00004550) throw new Error('Portable executable has no PE header');
  const coff = peOffset + 4;
  const sectionCount = u16(coff + 2);
  const optionalHeaderSize = u16(coff + 16);
  const optional = coff + 20;
  const magic = u16(optional);
  const dataDirectory = magic === 0x20b ? optional + 112 : magic === 0x10b ? optional + 96 : 0;
  if (!dataDirectory) throw new Error('Portable executable has an unknown optional header');
  const importRva = u32(dataDirectory + 8);
  const sectionTable = optional + optionalHeaderSize;
  const sections = [];
  for (let index = 0; index < sectionCount; index += 1) {
    const section = sectionTable + index * 40;
    sections.push({
      virtualAddress: u32(section + 12),
      rawAddress: u32(section + 20),
      span: Math.max(u32(section + 8), u32(section + 16)),
    });
  }
  const rvaToOffset = (rva) => {
    const section = sections.find(({ virtualAddress, span }) => rva >= virtualAddress && rva < virtualAddress + span);
    return section ? section.rawAddress + rva - section.virtualAddress : null;
  };
  const importOffset = rvaToOffset(importRva);
  if (importOffset === null) return [];
  const names = [];
  for (let index = 0; ; index += 1) {
    const descriptor = importOffset + index * 20;
    const nameRva = u32(descriptor + 12);
    if (!nameRva) break;
    const nameOffset = rvaToOffset(nameRva);
    if (nameOffset === null) throw new Error('Portable executable has an invalid import name');
    let end = nameOffset;
    while (bytes[end] !== 0) end += 1;
    names.push(bytes.toString('ascii', nameOffset, end));
  }
  return names;
}

const hostImports = importedDlls(await readFile(executable));
const machineRuntimeImports = hostImports.filter((name) => /^(?:vcruntime|ucrtbase|api-ms-win-crt)/i.test(name));
if (machineRuntimeImports.length) {
  throw new Error(`Portable host still imports machine CRT libraries: ${machineRuntimeImports.join(', ')}`);
}

await mkdir(output, { recursive: true });
await rm(join(output, 'extension'), { recursive: true, force: true });
await mkdir(join(output, 'extension'), { recursive: true });
await cp(executable, join(output, 'Download Manager.exe'));
await cp(extension, join(output, 'extension'), { recursive: true });

if (process.env.WEBVIEW2_FIXED_RUNTIME) {
  await rm(join(output, 'webview2'), { recursive: true, force: true });
  await cp(resolve(process.env.WEBVIEW2_FIXED_RUNTIME), join(output, 'webview2'), { recursive: true });
}

try {
  await access(join(output, 'webview2', 'msedgewebview2.exe'));
} catch {
  throw new Error('Portable packaging requires a colocated webview2 runtime');
}

const allowedEntries = new Set(['Download Manager.exe', 'extension', 'webview2']);
const unexpectedEntries = (await readdir(output)).filter((entry) => !allowedEntries.has(entry));
if (unexpectedEntries.length) {
  throw new Error(`Portable output contains unexpected entries: ${unexpectedEntries.join(', ')}`);
}
