# PSU Studio

[![Download PSU-Studio.exe](https://img.shields.io/badge/Download-PSU--Studio.exe-ffb020?style=for-the-badge&logo=windows&logoColor=black)](https://github.com/NB6RULES/OBRA-powersupply/releases/latest/download/PSU-Studio.exe)
[![Build](https://github.com/NB6RULES/OBRA-powersupply/actions/workflows/build-exe.yml/badge.svg)](https://github.com/NB6RULES/OBRA-powersupply/actions/workflows/build-exe.yml)

**[⬇ Download the latest Windows .exe](https://github.com/NB6RULES/OBRA-powersupply/releases/latest/download/PSU-Studio.exe)** · single file, no install. Windows may show a SmartScreen warning because it isn't code-signed: click *More info → Run anyway*.

![PSU Studio running a waveform](docs/screenshot.png)

A cleaner control app for the **Multicomp Pro MP711127 / MP711128 / MP711129 / MP711130 / MP711131** bench supplies. These are rebadged OWON SPE units. The app talks to the supply over USB using SCPI (CH340 serial, 115200 8N1).

## What it does

- **Big live readouts** for V, A and W, with a CV/CC badge and a trip warning.
- **Setpoints**: set voltage, current limit, OVP and OCP. Type a value and press Enter, use the +/− buttons, or Ctrl+scroll. Step size is selectable.
- **Output button**, plus **Esc as an emergency off**. Esc also stops a running waveform.
- **Power meter**: switch it on to track, while the output is on:
  - peak power, with the voltage, current and time it happened
  - average power and average current (time-weighted)
  - peak current, energy (Wh), charge (mAh) and output-on time
  - It flashes **NEW PEAK** when the record is beaten, and marks the peak on the live chart and in JPEG exports. It keeps counting while the chart is paused. Reset starts a new measurement.
  - Short spikes (inrush) can be missed between readings: raise Poll to 15 Hz to catch more.
- **8 presets**. Click one to load it. Right-click to save the current settings into it.
- **Live chart** with 30 s / 2 min / 10 min / all views and drag-to-zoom. It shows min/avg/max, energy in Wh and charge in mAh, and you can export the data to CSV.
- **Logging and export**:
  - **Record** writes every reading straight to `Documents\PSU Studio\logs\log_<date>.csv` as it arrives, so long runs are safe even if the app closes.
  - **Export CSV** on the Live tab saves everything collected since you connected (or pressed Clear).
  - Both CSVs have columns: timestamp, time_s, voltage_V, current_A, power_W, set_V, set_A.
  - **Save JPEG** on the Live and Waveform tabs saves the chart as a high-res image, with a title, stats, legend and date.
- **Waveform editor**:
  - Edit steps in a spreadsheet-style table (V, A, seconds, on/off), with a stepped preview and a play-head while it runs.
  - **Paste a lookup table**. Ctrl+V anywhere on the Waveform tab, or use the button. It accepts cells copied from Excel, CSV, tab-separated or space-separated text, and decimal commas.
  - **Import Excel/CSV**: .xlsx, .xlsm, .csv, .tsv, .txt. You can pick the sheet, or drag and drop a file onto the window.
  - Column mapper:
    - Assign each column a role: Time (absolute), Step duration, Voltage, Current, or Enabled. The roles are auto-guessed from the headers.
    - Pick units: s / ms / min, V / mV, A / mA.
    - Optional **resample** to an even step size, for dense LUTs.
  - **Generator** for sine, triangle, square, ramp, sawtooth and staircase waveforms.
  - Run options: loops (0 = forever), from/to step, and auto output on/off.
- **SCPI console** with command history.
- **Built-in driver install**:
  - The top bar shows the CH340 driver status.
  - "Install driver" runs the bundled installer with the Windows admin prompt.
  - The app then watches for the supply and connects automatically.
- **Simulator** so you can try everything without the supply.

## Run it (Windows)

1. Install Python 3.12 or 3.13 from python.org.
2. Double-click `run.bat`. It installs the 3 packages the first time.
3. To try it without hardware, double-click `run_simulator.bat`.

## Build the .exe yourself

Every push to `main` builds `PSU-Studio.exe` on GitHub Actions and publishes it as the latest release (the download button above). To build locally instead:

1. Put the CH340 driver installer in `drivers\`. This is the `CH341SER.EXE` or `CH34x_Install_Windows_v3_4.EXE` from the Multicomp zip.
2. Double-click `build.bat`.
3. The output is `dist\PSU Studio.exe`. That one file has the UI, the driver installer and everything else. Copy it anywhere.

If `drivers\` is empty, the Install driver button opens WCH's official download page instead.

## Notes

- Waveforms are timed by the PC, not by the supply. The SPE series has no list mode over SCPI. Steps under 50 ms get stretched to 50 ms. For dense lookup tables, use "Resample to 0.1 s".
- Close the Multicomp software first. Only one program can hold the COM port at a time.
- Settings and the last waveform are saved to `Documents\PSU Studio\settings.json`.
- If the desktop window won't open (very old Windows without WebView2), run `run.bat --browser`. It opens the same UI in your browser.
