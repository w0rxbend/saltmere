---
title: "Native USB on the ESP32-S3: CDC Serial and Drag-and-Drop Log Files with TinyUSB"
date: 2026-08-03
track: iot-embedded
summary: "The ESP32-S3's on-die USB On-The-Go peripheral and ESP-IDF's esp_tinyusb component expose an air-quality node as a CDC-ACM serial port with no bridge chip, or as a USB mass-storage disk from which CSV logs can be copied directly."
reading_time: 6
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

**Gist.** A sensor node that reaches a host through a USB-to-UART bridge chip (CP2102, CH340) pays for that chip in bill-of-materials cost, host driver variance, and a link whose throughput is bounded by the UART baud rate agreed between bridge and SoC. The ESP32-S3 carries a USB On-The-Go (OTG) controller on-die, and ESP-IDF's `esp_tinyusb` component drives it as arbitrary device classes — Communications Device Class, Abstract Control Model (CDC-ACM) for a serial console, Mass Storage Class (MSC) for a removable disk, or both in one composite device. The cost is an ownership constraint: **while the log partition is exposed over MSC, the firmware must not write to it**, because the host filesystem driver assumes exclusive control of the on-disk structures.

## Which variants carry the OTG controller

The single word "USB" covers two unrelated peripherals across the ESP32 family, and the distinction is a board-design decision rather than a firmware one.

Per Espressif's documentation, the **ESP32-S2** and **ESP32-S3** contain a full USB-OTG controller that TinyUSB drives, so they enumerate as arbitrary composite classes — CDC, MSC, Human Interface Device (HID), and combinations. Both are USB 2.0 **Full-Speed (12 Mbit/s)**. The **ESP32-P4** provides USB 2.0 **High-Speed (480 Mbit/s)**.

The RISC-V parts are where the assumption breaks. The **ESP32-C3**, **C6**, and **H2** carry only a fixed-function *USB Serial/JTAG* peripheral. That block is sufficient for flashing and for a console, but it is not the OTG controller, and TinyUSB's configurable classes — MSC in particular — do not run on it. The original **ESP32** has no USB peripheral at all. Mass storage therefore requires an S2, S3, or P4. The remainder assumes an S3.

## Installing the component and bringing up CDC-ACM

`esp_tinyusb` lives in the ESP Component Registry rather than the core IDF tree, so it is pulled in through the component manager:

```
idf.py add-dependency "espressif/esp_tinyusb"
```

The S3's D+ and D− signals are **fixed to GPIO20 and GPIO19** and routed to the internal PHY, so no pin matrix configuration exists to get wrong; the board must route those two pins, and a USB-C receptacle additionally needs the 5.1 kΩ configuration-channel (CC) pull-down resistors, without which a Type-C source will not supply VBUS.

A minimal CDC-ACM device installs the driver with a `tinyusb_config_t` — passing `NULL` for each descriptor field selects the `CONFIG_TINYUSB_DESC_*` values from menuconfig — and then initialises one ACM port:

```c
#include "tinyusb.h"
#include "tusb_cdc_acm.h"

static void cdc_rx_callback(int itf, cdcacm_event_t *event)
{
    uint8_t buf[CONFIG_TINYUSB_CDC_RX_BUFSIZE];
    size_t rx_size = 0;
    if (tinyusb_cdcacm_read(itf, buf, sizeof(buf), &rx_size) == ESP_OK) {
        // echo, or parse a command that triggers a log dump
        tinyusb_cdcacm_write_queue(itf, buf, rx_size);
        tinyusb_cdcacm_write_flush(itf, 0);
    }
}

void app_main(void)
{
    const tinyusb_config_t tusb_cfg = {
        .device_descriptor = NULL,        // use CONFIG_TINYUSB_DESC_* defaults
        .string_descriptor = NULL,
        .external_phy = false,            // the S3 has an internal PHY
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

Two details in that structure are load-bearing. `.external_phy = false` selects the S3's on-chip PHY; an external PHY is a different board topology. `rx_unread_buf_sz` sizes the buffer that holds bytes already accepted from the bus but not yet drained by `tinyusb_cdcacm_read` — **a callback that reads less than the endpoint delivered leaves the remainder queued**, so the read loop must consume until `rx_size` reports zero rather than assuming one callback maps to one message.

Where only console output is wanted, the callbacks can be omitted entirely: `esp_tusb_init_console(TINYUSB_CDC_ACM_0)` redirects `stdin`, `stdout` and `stderr` to the CDC port, and `esp_tusb_deinit_console()` returns them to the UART. The trade-off runs the other way from a bridge chip: the bridge's serial port exists whenever the bridge is powered, whereas a CDC port implemented by the application disappears from the host whenever the device resets or the firmware stops servicing USB, and reappears only after re-enumeration. Host terminals that do not reopen a vanished port therefore need reattaching after every reset.

## MSC: the node as a removable disk

Mass Storage is the class that changes the field workflow. `esp_tinyusb` ships two storage backends, both demonstrated in the `tusb_msc` example: **internal SPI flash accessed through the wear-levelling layer**, and an **SD/MMC card**. One backend is initialised, and TinyUSB presents that partition to the host as a removable disk.

For a node that already maintains a FAT partition of CSV logs, the SPI-flash path is:

```c
#include "tusb_msc_storage.h"

// wl_handle from wl_mount() / esp_vfs_fat_spiflash_mount_rw_wl()
const tinyusb_msc_spiflash_config_t msc_cfg = { .wl_handle = wl_handle };
ESP_ERROR_CHECK(tinyusb_msc_storage_init_spiflash(&msc_cfg));
```

The SD/MMC path has the same shape: `tinyusb_msc_storage_init_sdmmc()` with a `tinyusb_msc_sdmmc_config_t { .card = card }`, where `card` is the handle produced by mounting the card. SD is preferable when log volume is large — a long run of one-minute particulate-matter (PM2.5) samples accumulates steadily — and it keeps the write traffic off the internal flash that also holds the application image.

The mechanism worth understanding is that **MSC is a block device, not a file server**. The host issues SCSI read and write commands against logical block addresses; the FAT driver interpreting those blocks runs on the host, not on the node. The node contributes no knowledge of files, directories, or allocation tables. Two consequences follow directly.

First, **the host and the firmware cannot both own the filesystem simultaneously**. The host caches directory entries and file-allocation-table sectors and writes them back on its own schedule; concurrent firmware writes to the same partition are invisible to that cache, and the writeback overwrites them. The `tusb_msc` example models the required discipline explicitly with `expose` and `status` commands: expose the partition to retrieve logs, unexpose to resume logging. A practical trigger is VBUS presence or a CDC line-state change — on host attach, unmount application-side and hand the partition to MSC; on detach, remount and resume sampling.

Second, **the wear-levelling layer sits underneath the block interface**, so the logical sector numbers the host addresses are remapped before reaching physical flash. This is transparent to the host, but it means the exposed volume must be reached through `wl_handle` rather than through raw partition offsets.

Keeping the log partition separate from configuration and state — a small LittleFS partition serves for the latter — restricts the drag-and-drop volume to the CSV files alone, so a host that reorganises or repairs the volume cannot touch device state.

## What the S3 buys

The bridge chip, its passives, and its host-driver dependency disappear. Retrieval stops requiring a serial terminal and a scripted dump: a node visited in the field is plugged into any laptop and the file is copied. For a wall-mounted sensor node visited on a quarterly cadence, that last property is the operational argument for the part.

## Pitfalls

- **Selecting a C3, C6, or H2 for a mass-storage design.** Enumeration as a disk never occurs, because those parts expose a fixed-function USB Serial/JTAG block rather than the OTG controller TinyUSB requires.
- **Writing to the log partition while it is exposed over MSC.** The host's cached file-allocation-table and directory sectors are written back over the firmware's changes, producing truncated or corrupt CSV files with no error reported on either side.
- **Omitting the 5.1 kΩ CC pull-downs on a USB-C receptacle.** A Type-C source never asserts VBUS, so the board appears dead on that cable while working normally on a Type-A adapter.
- **Expecting the CDC port to persist across a reset.** The device implements the port itself, so resetting it removes the port from the host until enumeration completes again, and a terminal holding the old handle stops receiving output.
- **Treating one CDC receive callback as one message.** Bytes beyond what a single `tinyusb_cdcacm_read` consumes remain in the unread buffer, so commands split across endpoint transfers are silently truncated.
- **Exposing a partition that also holds configuration or state.** A host filesystem repair or a user deleting "extra" files removes device state along with the logs.
- **Assuming Full-Speed throughput scales to High-Speed figures.** S2 and S3 negotiate 12 Mbit/s; only the P4 offers 480 Mbit/s, so transfer time for a large log is bounded accordingly.
