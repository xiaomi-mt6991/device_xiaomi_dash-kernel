#!/usr/bin/env python3

import os
import sys
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

EXTRACT_OTA = "../../../prebuilts/extract-tools/linux-x86/bin/ota_extractor"
MKDTBOIMG = "../../../system/libufdt/utils/src/mkdtboimg.py"
UNPACKBOOTIMG = "../../../system/tools/mkbootimg/unpack_bootimg.py"

BLACKLISTED_MODULES = {
    "libarc4.ko",
    "rfkill.ko",
    "simtray.ko",
    "perf_helper.ko",
    "mi_mempool.ko",
    "migt.ko",
    "unfairmem.ko",
    "scene_swappiness.ko",
    "unionpower.ko",
    "binder_prio.ko",
    "sla.ko",
    "miwill.ko",
    "miwill_mode_redudancy.ko",
    "minet.ko",
    "mi_t1_gpio.ko",
    "millet_binder.ko",
    "millet_core.ko",
    "millet_hs.ko",
    "millet_pkg.ko",
    "millet_sig.ko",
    "millet_oem_cgroup.ko",
    "binder_gki.ko",
    "dio_dma_mapper.ko",
    "mi_log.ko",
    "mi_exception_log.ko",
    "mi_mem_epoll.ko",
    "mi_stack.ko",
    "mi_ubt.ko",
    "mi_ubt_test.ko",
    "bootmonitor.ko",
    "crash_module.ko",
    "zsmalloc.ko",
    "zram.ko",
}

extract_out = None


def error_handler():
    if extract_out and Path(extract_out).is_dir():
        print(f"Error detected, cleaning temporal working directory {extract_out}")
        shutil.rmtree(extract_out)


def usage():
    print("Usage: ./extract-files.py <rom-zip>")
    sys.exit(1)


def get_path(name: str) -> str:
    return str(Path(extract_out) / name)


def run(*args, **kwargs):
    subprocess.run(list(args), check=True, **kwargs)


def unpackbootimg(*args):
    run("python3", UNPACKBOOTIMG, *args)


def extract_ota(*args):
    run(EXTRACT_OTA, *args)


def strip_blacklisted_modules(modules_dir: Path, keep: set = frozenset()):
    if not modules_dir.is_dir():
        return

    to_remove = BLACKLISTED_MODULES - keep

    removed_any = False
    for ko in modules_dir.rglob("*.ko"):
        if ko.name in to_remove:
            ko.unlink()
            print(f"  - removed {ko.relative_to(modules_dir)}")
            removed_any = True

    for list_file in list(modules_dir.rglob("modules.load*")) + list(modules_dir.rglob("modules.blocklist")):
        if not list_file.is_file():
            continue
        lines = list_file.read_text().splitlines()
        kept = [l for l in lines if Path(l.strip()).name not in to_remove]
        if len(kept) != len(lines):
            list_file.write_text("\n".join(kept) + ("\n" if kept else ""))
            print(f"  - cleaned {list_file.relative_to(modules_dir)}")
            removed_any = True

    if not removed_any:
        print(f"  - nothing to remove in {modules_dir}")


def main():
    global extract_out

    if len(sys.argv) < 2:
        usage()

    rom_zip = sys.argv[1]

    if not Path(UNPACKBOOTIMG).is_file():
        print(f"Missing {UNPACKBOOTIMG}, are you on the correct directory?")
        sys.exit(1)

    if not Path(EXTRACT_OTA).is_file():
        print(f"Missing {EXTRACT_OTA}, are you on the correct directory and have built the ota_extractor target?")
        sys.exit(1)

    if not Path(rom_zip).is_file():
        usage()

    # Clean and create needed directories
    for d in ["./modules/vendor_dlkm", "./modules/system_dlkm", "./modules/vendor_ramdisk", "./dtb"]:
        shutil.rmtree(d, ignore_errors=True)
        Path(d).mkdir(parents=True)

    extract_out = tempfile.mkdtemp()
    print(f"Using {extract_out} as working directory")

    try:
        # Extract the OTA package
        print(f"Extracting the payload from {rom_zip}")
        run("unzip", rom_zip, "payload.bin", "-d", extract_out)

        print("Extracting OTA images")
        extract_ota(
            "-payload", get_path("payload.bin"),
            "-output_dir", extract_out,
            "-partitions", "boot,dtbo,vendor_boot,vendor_dlkm,system_dlkm",
        )

        # BOOT
        print("Extracting the kernel image from boot.img")
        boot_out = Path(extract_out) / "boot-out"
        boot_out.mkdir()
        print(f"Extracting at {boot_out}")
        unpackbootimg("--boot_img", get_path("boot.img"), "--out", str(boot_out), "--format", "mkbootimg")
        print("Done. Copying the kernel as Image.lz4")
        shutil.copy(boot_out / "kernel", "./Image.lz4")
        print("Done")

        # VENDOR_BOOT
        print("Extracting the ramdisk kernel modules and DTB")
        vb_out = Path(extract_out) / "vendor_boot-out"
        vb_out.mkdir()
        print(f"Extracting at {vb_out}")
        unpackbootimg("--boot_img", get_path("vendor_boot.img"), "--out", str(vb_out), "--format", "mkbootimg")

        print("Done. Extracting the ramdisk")
        ramdisk_dir = vb_out / "ramdisk"
        ramdisk_dir.mkdir()
        ramdisk_lz4 = vb_out / "vendor_ramdisk00"
        ramdisk_raw = vb_out / "vendor_ramdisk"
        run("unlz4", str(ramdisk_lz4), str(ramdisk_raw))
        run("cpio", "-i", "-F", str(ramdisk_raw), "-D", str(ramdisk_dir))

        print("Copying all ramdisk modules")
        for module in ramdisk_dir.rglob("*"):
            if module.is_file() and (
                module.suffix == ".ko"
                or module.name.startswith("modules.load")
                or module.name == "modules.blocklist"
            ):
                shutil.copy(module, "./modules/vendor_ramdisk/")

        print("Stripping blacklisted modules from vendor_ramdisk")
        strip_blacklisted_modules(Path("./modules/vendor_ramdisk"))

        # VENDOR_DLKM
        print("Extracting the dlkm kernel modules")
        vdlkm_out = Path(extract_out) / "vendor_dlkm"
        print(f"Extracting at {vdlkm_out}")
        run("fsck.erofs", f"--extract={vdlkm_out}", get_path("vendor_dlkm.img"))
        print("Done. Extracting the vendor dlkm")

        print("Copying all vendor dlkm modules")
        for module in (vdlkm_out / "lib").rglob("*"):
            if module.is_file() and (
                module.suffix == ".ko"
                or module.name.startswith("modules.load")
                or module.name == "modules.blocklist"
            ):
                shutil.copy(module, "./modules/vendor_dlkm/")

        print("Stripping blacklisted modules from vendor_dlkm")
        strip_blacklisted_modules(Path("./modules/vendor_dlkm"))

        # SYSTEM_DLKM
        print("Extracting the system dlkm kernel modules")
        sdlkm_out = Path(extract_out) / "system_dlkm"
        print(f"Extracting at {sdlkm_out}")
        run("fsck.erofs", f"--extract={sdlkm_out}", get_path("system_dlkm.img"))
        print("Done. Extracting the system dlkm")

        print("Copying all system dlkm modules")
        modules_root = sdlkm_out / "lib" / "modules"
        for ver_dir in modules_root.iterdir():
            if ver_dir.is_dir():
                for item in ver_dir.iterdir():
                    dest = Path("./modules/system_dlkm") / item.name
                    if item.is_dir():
                        shutil.copytree(item, dest, dirs_exist_ok=True)
                    else:
                        shutil.copy(item, dest)
            else:
                shutil.copy(ver_dir, Path("./modules/system_dlkm") / ver_dir.name)

        print("Stripping blacklisted modules from system_dlkm")
        strip_blacklisted_modules(
            Path("./modules/system_dlkm"),
            keep={"libarc4.ko", "rfkill.ko", "zsmalloc.ko", "zram.ko"},
        )

        # Extract DTBO and DTBs
        print("Extracting DTBO and DTBs")
        extract_dtb_py = Path(extract_out) / "extract_dtb.py"
        url = "https://raw.githubusercontent.com/PabloCastellano/extract-dtb/master/extract_dtb/extract_dtb.py"
        urllib.request.urlretrieve(url, extract_dtb_py)

        dtbs_out = Path(extract_out) / "dtbs"
        run("python3", str(extract_dtb_py), str(vb_out / "dtb"), "-o", str(dtbs_out))

        for dtb in dtbs_out.rglob("*.dtb"):
            shutil.copy(dtb, "./dtb/")
            print(f"  - dtb/{dtb.name}")

        print("Done")

    except Exception:
        error_handler()
        raise

    shutil.rmtree(extract_out)
    print("Extracted files successfully")


if __name__ == "__main__":
    main()
