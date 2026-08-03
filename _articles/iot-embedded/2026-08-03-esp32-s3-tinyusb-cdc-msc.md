---
title: "Native USB on the ESP32-S3: CDC Serial and Drag-and-Drop Log Files with TinyUSB"
date: 2026-08-03
track: iot-embedded
summary: "Use the ESP32-S3's built-in USB-OTG peripheral and ESP-IDF's esp_tinyusb component to expose an air-quality node as a CDC-ACM serial port with no bridge chip, or as a USB mass-storage disk you can drag CSV logs off of."
reading_time: 5
tags: [esp32, esp32-s3, tinyusb, usb, cdc, msc, esp-idf, air-quality]
sources:
  - title: "ESP-IDF Programming Guide — USB Device Stack (ESP32-S3)"
    url: "https://docs.espressif.com/projects/esp-idf/en/stable/esp32s3/api-reference/peripherals/usb_device.html"
  - title: "esp_tinyusb — ESP Component Registry"
    url: "https://components.espressif.com/components/espressif/esp_tinyusb"
  - title: "ESP-IDF example: TinyUSB Mass Storage Device (tusb_msc)"
    url: "https://github.com/espressif/esp-idf/blob/master/examples/peripherals/usb/device/tusb_msc/README.md"
  - title: "ESP-FAQ — USB peripheral support across ESP32 variants"
    url: "https://docs.espressif.com/projects/esp-faq/en/latest/software-framework/peripherals/usb.html"
  - title: "TinyUSB documentation"
    url: "https://docs.tinyusb.org/en/latest/"
---

Most of my sensor nodes talk to a laptop through a CP2102 or CH340 — a little USB-UART bridge sitting between the ESP32 and the host. It works, but it is another part to place, another driver story on Windows, and it caps you at whatever the bridge negotiates. The ESP32-S3 makes that chip optional. It has a real USB 2.0 OTG controller on-die, and with ESP-IDF's `esp_tinyusb` component you can present the board to a host as whatever USB device class you want: a serial port, a flash drive, or both at once. For an air-quality node that logs CSV to flash, the flash-drive part is the interesting one — you plug in the node and drag the log file off like a thumb drive.

## Which chips actually have native USB

This matters before you design a board, because the marketing term "USB" hides two very different peripherals. Per Espressif's docs, the **ESP32-S2** and **ESP32-S3** have a full USB-OTG controller that TinyUSB drives, so they can enumerate as arbitrary composite device classes — CDC, MSC, HID, and so on. The **ESP32-P4** goes further with USB 2.0 High-Speed (480 Mbps); S2/S3 are Full-Speed (12 Mbps).

The RISC-V parts are the trap. The **ESP32-C3**, **C6**, and **H2** only have a fixed-function *USB Serial/JTAG* peripheral — great for flashing and a console, but it is not the OTG controller and TinyUSB's configurable classes (MSC in particular) do not run on it. The original **ESP32** has no USB at all. So: if you want the mass-storage trick, you need an S2, S3, or P4. Everything below assumes an S3.

## Wiring up the component and CDC-ACM

`esp_tinyusb` is a managed component, not part of the core IDF tree, so you pull it in with the component manager:

```
idf.py add-dependency "espressif/esp_tinyusb"
```

The S3's USB D+/D- come out on GPIO20/GPIO19 (fixed to the internal PHY), so there is no pin config to do — just make sure your board routes them and, ideally, has the 5.1 kΩ CC resistors if you went with USB-C.

A minimal CDC-ACM serial device looks like this. Install the driver with a `tinyusb_config_t` (passing `NULL` descriptors gets you sensible defaults from menuconfig), then bring up one ACM port:

```c
#include "tinyusb.h"
#include "tusb_cdc_acm.h"

static void cdc_rx_callback(int itf, cdcacm_event_t *event)
{
    uint8_t buf[CONFIG_TINYUSB_CDC_RX_BUFSIZE];
    size_t rx_size = 0;
    if (tinyusb_cdcacm_read(itf, buf, sizeof(buf), &rx_size) == ESP_OK) {
        // echo back, or parse a command to trigger a log dump
        tinyusb_cdcacm_write_queue(itf, buf, rx_size);
        tinyusb_cdcacm_write_flush(itf, 0);
    }
}

void app_main(void)
{
    const tinyusb_config_t tusb_cfg = {
        .device_descriptor = NULL,        // use CONFIG_TINYUSB_DESC_* defaults
        .string_descriptor = NULL,
        .external_phy = false,            // S3 has an internal PHY
        .configuration_descriptor = NULL,
    };
    ESP_ERROR_CHECK(tinyusb_driver_install(&tusb_cfg));

    const tinyusb_config_cdcacm_t acm_cfg = {
        .usb_dev = TINYUSB_USBDEV_0,
        .cdc_port = TINYUSB_CDC_ACM_0,
        .rx_unread_buf_sz = 64,
        .callback_rx = &cdc_rx_callback,
        .callback_rx_wanted_char = NULL,
        .callback_line_state_changed = NULL,
        .callback_line_coding_changed = NULL,
    };
    ESP_ERROR_CHECK(tusb_cdc_acm_init(&acm_cfg));
}
```

If all you want is your `printf` and `ESP_LOGI` output on that port instead of the UART, skip the callbacks and call `esp_tusb_init_console(TINYUSB_CDC_ACM_0)` after init — it redirects stdin/stdout/stderr to CDC, and `esp_tusb_deinit_console()` puts them back on UART. That alone gets you a robust console with no bridge chip and a proper reset that does not glitch on connect.

## MSC: the node as a flash drive

The class that changes the field-workflow is Mass Storage. `esp_tinyusb` ships two storage backends, shown in the `tusb_msc` example: internal SPI flash through the wear-levelling layer, and an SD/MMC card. You initialise one of them and TinyUSB presents that partition to the host as a removable disk.

For an air-quality node that already keeps a FAT partition of CSV logs, the SPI-flash path is:

```c
#include "tusb_msc_storage.h"

// wl_handle from wl_mount() / esp_vfs_fat_spiflash_mount_rw_wl()
const tinyusb_msc_spiflash_config_t msc_cfg = { .wl_handle = wl_handle };
ESP_ERROR_CHECK(tinyusb_msc_storage_init_spiflash(&msc_cfg));
```

An SD card is the same shape with `tinyusb_msc_storage_init_sdmmc()` and a `tinyusb_msc_sdmmc_config_t { .card = card }`, where `card` is the handle from mounting the card. SD is the better choice if the logs are large — months of one-minute PM2.5 samples add up, and you are not burning cycles on the internal flash.

The one rule to internalise: **the host and the firmware cannot both own the filesystem at once.** While the disk is exposed over USB, the host's OS assumes exclusive control of the FAT structures, so your app must stop writing to that partition. The example models this with `expose`/`status` commands — expose the disk to dump logs, then unexpose to resume logging. In practice I gate it on VBUS or a "host connected" line-state event: when a PC is plugged in, unmount app-side and hand the partition to MSC; on unplug, remount and keep sampling. Keep the log partition separate from any config/state you store elsewhere (a small LittleFS partition is fine for that) so the drag-and-drop volume is *only* the CSVs.

## Why this is worth the S3 premium

You drop the bridge chip, its passives, and its driver headaches. The serial console survives resets cleanly because the USB stack re-enumerates instead of dropping a UART. And field retrieval stops meaning "attach a serial terminal, script a dump, hope the transfer completes" — a non-technical person can plug the node into any laptop and copy a file. For a battery-and-flash sensor node that lives on a wall for months and gets visited quarterly, that last one is the whole reason to do it.

**Try next:** flash the stock `tusb_msc` example onto an S3 devkit, point it at a FAT partition with a dummy `log.csv`, and confirm the file appears on your desktop — then wire the expose/unexpose to a VBUS-detect GPIO so the node logs when unplugged and mounts as a disk when you connect it.
