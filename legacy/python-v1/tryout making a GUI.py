import numpy as np
import pandas as pd
import tkinter as tk
import serial
import time
import matplotlib.pyplot as plt
from IPython import display
from datetime import datetime
import os 
import sys
import re
import signal

def dark_measurement():
    PORT = 'COM10'  # Replace with your serial port
    num_runs = runs.get()  # Get the value from the entry widget

    with serial.Serial(PORT, baudrate=115200, timeout=5) as ser:
        time.sleep(2)  # Wait for the serial connection to initialize

        input("Press Enter when ready to take dark spectrum measurement...")
        ser.write(b"dark\n")
        time.sleep(0.1)  # Wait for the device to respond
        dark_raw = ser.readline()
        print(dark_raw.decode().strip())

def regular_measurement():
    PORT = 'COM10'  # Replace with your serial port
    num_runs = runs.get()  # Get the value from the entry widget

    with serial.Serial(PORT, baudrate=115200, timeout=5) as ser:
        time.sleep(2)  # Wait for the serial connection to initialize

        input("Press Enter when ready to take measurement...")
        
        with open("spec.csv", "a") as f:
            for i in range(num_runs):
                ser.write(b"spec\n")
                time.sleep(0.1)  # Wait for the device to respond
                spec_raw = ser.readline()
                print(spec_raw.decode().strip())
                f.write(spec_raw.decode().strip() + "\n")


def plot_csv():
    data = pd.read_csv("spec read.csv")
    for i in range(len(data)-runs.get(), len(data)):
        y = data.iloc[i, 1:]
        x = np.linspace(340, 780, len(y))
        timestamp = data.iloc[i, 0]
        plt.plot(x, y, label=f"{timestamp}")
    plt.legend()
    plt.show()

root = tk.Tk()
root.title("C12666 Spectrometer GUI")

label = tk.Label(root, text="C12666 Spectrometer GUI")
label.grid(column=2, row=3, padx=10, pady=(10, 5), sticky=tk.W)

mainframe = tk.Frame(root)
mainframe.grid(column=0, row=1, sticky=(tk.N, tk.W, tk.E, tk.S), padx=10, pady=5)

button = tk.Button(mainframe, text="Dark Measurement", command=dark_measurement)
button.grid(column=0, row=0, sticky=tk.W, padx=5, pady=5)

button2 = tk.Button(mainframe, text="Regular Measurement", command=regular_measurement)
button2.grid(column=1, row=0, sticky=tk.W, padx=5, pady=5)

plot_button = tk.Button(mainframe, text="Plot CSV", command=plot_csv)
plot_button.grid(column=0, row=1, sticky=tk.W, padx=5, pady=5)

runs = tk.IntVar()
runs_entry = tk.Entry(mainframe, width=7, textvariable=runs)
runs_entry.grid(column=1, row=1, sticky=(tk.W, tk.E), padx=5, pady=5)

root.mainloop()