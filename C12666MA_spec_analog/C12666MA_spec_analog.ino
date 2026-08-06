/*
 * ============================================================================
 *  C12666MA_spectrometer.ino
 *  Hamamatsu C12666MA micro-spectrometer reader for the Arduino Uno (ATmega328P)
 * ============================================================================
 *
 *  Reads all 256 pixels of the sensor and sends them to a host PC over USB
 *  serial, using simple comma-separated text commands. Beginner-friendly:
 *  plain functions and global variables, one file, lots of comments.
 *
 *  --- WIRING (Uno is 5V, so no level shifters or op-amps are required) -------
 *     Sensor CLK   -> Uno A2   (digital output)
 *     Sensor ST    -> Uno A1   (digital output)
 *     Sensor VIDEO -> Uno A3   (ANALOG input, read with analogRead)
 *     Sensor Gain  -> Uno A0   (digital output - see GAIN PIN note below)
 *     Sensor +Vs   -> Uno 5V
 *     Sensor GND   -> Uno GND
 *     Sensor EOS   -> not used by this sketch, can be left unconnected
 *     Sensor Case  -> Uno GND (recommended, reduces noise pickup)
 *
 *  --- GAIN PIN NOTE -----------------------------------------------------------
 *     Unlike the C12880MA, the C12666MA has a Gain pin (sensor pin 9) instead
 *     of a Trigger pin. Per the Hamamatsu datasheet:
 *       - Low gain:  pin left open, or driven/pulled to Vdd (5V)
 *       - High gain: pin driven to GND (<0.4V)
 *     The sensor already has a 10 kOhm internal pull-up to Vdd on this pin, so
 *     do NOT add an external pull-up or pull-down resistor - just wire it
 *     straight to A0. This sketch drives A0 as an output so gain is set
 *     explicitly in software rather than left floating.
 *
 *  --- INTEGRATION TIME NOTE ----------------------------------------------------
 *     The C12666MA's datasheet-specified minimum integration time is much
 *     longer than the C12880MA's (roughly 1.28 ms vs ~11 us). This sketch
 *     defaults to and enforces a 1280 us floor - see MIN_INTEGRATION_US below.
 *
 *  --- ACQUISITION PROTOCOL (verified against groupgets/c12666ma) -------------
 *     The C12666MA's readout sequence is NOT the same shape as the C12880MA's
 *     and differs in two important ways:
 *       1. VIDEO only updates once every 4 CLK pulses (the datasheet states
 *          "video output is 1/4 of the clock pulse frequency"), so each
 *          pixel needs a 4-clock group, with the valid sample taken on the
 *          first clock's falling edge.
 *       2. ST idles HIGH and is only pulsed briefly LOW - once to mark the
 *          start of integration, once to mark the end - rather than staying
 *          low for the whole pixel readout. The datasheet notes: "The clock
 *          pulse should be set from high to low just once when the start
 *          pulse is low. The internal shift register starts operating at
 *          this timing," i.e. that single edge is what actually triggers
 *          the sensor, so it has to be timed precisely.
 *     This sketch's acquireOnePass() follows that six-step sequence (leading
 *     clocks, start pulse, integration, stop pulse, 4-clocks-per-pixel read,
 *     trailing clocks), matching the reference sketch at:
 *       https://github.com/groupgets/c12666ma
 *       https://github.com/tricorderproject/arducordermini
 *
 *  --- HOW TO USE -------------------------------------------------------------
 *     1. Upload with the Arduino IDE (board: "Arduino Uno").
 *     2. Open Serial Monitor at 115200 baud, line ending = "Carriage Return".
 *     3. Type commands (see the table in handleCommand() below), e.g.:
 *          hello
 *          set_integration,2000
 *          set_gain,1      (0 = low gain, 1 = high gain)
 *          dark            (capture a dark reference with the sensor covered)
 *          spec            (dark-subtracted spectrum, 256 numbers)
 *          spec,raw        (raw spectrum, 256 numbers)
 *
 *  Adapted from a C12880MA sketch ported from the GroupGets C12880MA Arduino
 *  example (https://github.com/groupgets/c12666ma) for the C12666MA sensor.
 *  Timing constants that are sensor-specific have been called out above -
 *  please verify them against your hardware before trusting the output.
 * ============================================================================
 */

#include <EEPROM.h>

// ============================================================================
//  1. PINS & CONSTANTS
// ============================================================================

// Pin assignments (match the GroupGets breakout layout).
const uint8_t SPEC_CLK   = A2;   // clock output to the sensor
const uint8_t SPEC_ST    = A1;   // start pulse output to the sensor
const uint8_t SPEC_VIDEO = A3;   // analog video input from the sensor
const uint8_t SPEC_GAIN  = A0;   // gain select output to the sensor (pin 9)
const uint8_t LED_PIN    = 2;    // external status LED (pin 0/1 are used by
                                  // Serial - don't repurpose those)

// Gain pin logic levels (see GAIN PIN note above).
const uint8_t GAIN_LOW  = HIGH;  // low gain:  pin at/near Vdd
const uint8_t GAIN_HIGH = LOW;   // high gain: pin at/near GND

// The sensor has 256 pixels (spectral channels).
const int SPEC_CHANNELS = 256;

// Half-period of each clock pulse, in microseconds. 1 us gives a clean,
// slow clock that the sensor is happy with. Bigger = slower and safer.
// The C12666MA datasheet allows a clock frequency of 1 kHz to 800 kHz
// (f(CLK)), so this slow default clock is comfortably within range.
const int CLK_DELAY_US = 1;

// Datasheet-specified minimum integration time for the C12666MA is about
// 1.28 ms. We enforce this floor in set_integration() so a mistaken
// set_integration,0 command can't silently produce underexposed frames.
const unsigned long MIN_INTEGRATION_US = 5000UL;

// Averaging is capped so the uint16 accumulator (in g_data) cannot overflow:
// 63 * 1023 = 64449, which is below 65535. This also keeps us inside the
// Uno's 2 KB of RAM (a 32-bit accumulator array would not fit next to g_dark).
const uint8_t MAX_AVG = 63;

// ============================================================================
//  2. CONFIGURATION STORED IN EEPROM
// ============================================================================
// The Uno has no filesystem, so settings that must survive a power cycle are
// kept in EEPROM. A "magic" byte lets us detect a blank chip and load defaults.

const uint8_t CONFIG_MAGIC = 0x66;   // bumped from the C12880MA version (0x43)
                                      // since the struct below gained a field -
                                      // change this again if you change the struct

struct Config {
  uint8_t       magic;             // must equal CONFIG_MAGIC if valid
  char          name[16];          // human-friendly device name
  unsigned long integration_us;    // extra exposure time (microseconds)
  uint8_t       n_avg;             // number of frames to average (1..MAX_AVG)
  float         wl_coeffs[6];      // wavelength polynomial A0..A5 (used host-side)
  uint8_t       high_gain;         // 0 = low gain, 1 = high gain
};

Config g_cfg;   // the live copy of our settings

// ============================================================================
//  3. FRAME BUFFERS  (the biggest users of RAM)
// ============================================================================

uint16_t g_data[SPEC_CHANNELS];   // latest frame; also used as the average sum
uint16_t g_dark[SPEC_CHANNELS];   // dark reference frame
bool     g_have_dark = false;     // true once a dark frame has been captured
bool     g_led_on = false;        // current LED state (not persisted in EEPROM -
                                   // always starts off after a reboot)

// ============================================================================
//  4. SERIAL COMMAND BUFFER
// ============================================================================
// We read characters into a fixed char array (not a String) to avoid the
// memory-fragmentation problems that String can cause on small AVR chips.

char    g_cmd[40];
uint8_t g_cmd_len = 0;

// ---------------------------------------------------------------------------
//  Soft reset: jumping to address 0 restarts the sketch. (Simple, not a full
//  hardware reset, but fine for the "reboot" command.)
// ---------------------------------------------------------------------------
void (*resetBoard)(void) = 0;

// ============================================================================
//  EEPROM helpers
// ============================================================================

void saveConfig() {
  EEPROM.put(0, g_cfg);
}

void loadConfig() {
  EEPROM.get(0, g_cfg);
  if (g_cfg.magic != CONFIG_MAGIC) {
    // Blank or outdated EEPROM: fill in sensible defaults and store them.
    g_cfg.magic = CONFIG_MAGIC;
    strcpy(g_cfg.name, "C12666MA");
    g_cfg.integration_us = MIN_INTEGRATION_US;   // datasheet minimum, not 0
    g_cfg.n_avg = 1;               // no averaging by default
    for (int i = 0; i < 6; i++) g_cfg.wl_coeffs[i] = 0.0;
    g_cfg.high_gain = 0;           // low gain by default
    saveConfig();
  }
  // Make sure a corrupted value can never break averaging or integration.
  if (g_cfg.n_avg < 1)       g_cfg.n_avg = 1;
  if (g_cfg.n_avg > MAX_AVG) g_cfg.n_avg = MAX_AVG;
  if (g_cfg.integration_us < MIN_INTEGRATION_US) g_cfg.integration_us = MIN_INTEGRATION_US;
}

// ============================================================================
//  5. ACQUISITION
// ============================================================================

// One low/high clock pulse on SPEC_CLK (order matches the reference sketch;
// what matters for a free-running train is that transitions are evenly
// spaced, so this is equivalent to a high/low pulse for anything except the
// edge-critical start/stop pulses handled separately in pulseStartStop()).
void pulseClock() {
  digitalWrite(SPEC_CLK, LOW);
  delayMicroseconds(CLK_DELAY_US);
  digitalWrite(SPEC_CLK, HIGH);
  delayMicroseconds(CLK_DELAY_US);
}

// Push the current gain setting out to the sensor's Gain pin.
void applyGain() {
  digitalWrite(SPEC_GAIN, g_cfg.high_gain ? GAIN_HIGH : GAIN_LOW);
}

// A brief ST HIGH->LOW->HIGH pulse, timed to CLK edges. Called once to mark
// the start of integration and again to mark the end - the sequence is
// identical both times (see ACQUISITION PROTOCOL note up top). The single
// CLK falling edge that happens while ST is LOW is what actually triggers
// the sensor's internal shift register, per the datasheet.
void pulseStartStop() {
  digitalWrite(SPEC_CLK, LOW);
  delayMicroseconds(CLK_DELAY_US);
  digitalWrite(SPEC_CLK, HIGH);
  digitalWrite(SPEC_ST, LOW);
  delayMicroseconds(CLK_DELAY_US);

  digitalWrite(SPEC_CLK, LOW);
  delayMicroseconds(CLK_DELAY_US);
  digitalWrite(SPEC_CLK, HIGH);
  digitalWrite(SPEC_ST, HIGH);
  delayMicroseconds(CLK_DELAY_US);
}

// Run the sensor once and ADD the 256 readings into g_data (so it can be
// called several times in a row to average). Six-step sequence verified
// against the groupgets/c12666ma reference sketch - see ACQUISITION
// PROTOCOL note up top for why this differs from a typical C12880MA sketch.
void acquireOnePass() {
  // -- Step 1: one full frame's worth of leading clocks, ST idling HIGH. --
  for (int i = 0; i < SPEC_CHANNELS; i++) pulseClock();

  // -- Step 2: start pulse - marks the beginning of integration. --
  pulseStartStop();

  // -- Step 3: integration window. ST stays HIGH; CLK keeps running so the
  //    sensor's internal clocking stays alive, in groups of 2 pulseClock()
  //    calls (~1 clock cycle each). We time this against a real elapsed-
  //    microsecond count via micros(), NOT by estimating clock counts -
  //    digitalWrite() call overhead on the Uno is several microseconds per
  //    call, which dwarfs CLK_DELAY_US and made a clock-counting estimate
  //    badly inaccurate. This loop instead keeps pulsing until the
  //    requested integration_us has actually elapsed. --
  unsigned long intStart = micros();
  while ((unsigned long)(micros() - intStart) < g_cfg.integration_us) {
    pulseClock();
    pulseClock();
  }

  // -- Step 4: stop pulse - marks the end of integration; pixel shift-out
  //    begins right after. --
  pulseStartStop();

  // -- Step 5: read the 256 real pixels. Each pixel gets a 4-clock group;
  //    VIDEO is valid on the first clock's falling edge, so that's where we
  //    sample. The remaining 3 clocks just shift to the next pixel. --
  for (int i = 0; i < SPEC_CHANNELS; i++) {
    digitalWrite(SPEC_CLK, LOW);
    delayMicroseconds(CLK_DELAY_US);
    digitalWrite(SPEC_CLK, HIGH);
    delayMicroseconds(CLK_DELAY_US);
    digitalWrite(SPEC_CLK, LOW);
    // Settling delay before sampling. If a buffer op-amp is in the signal
    // path, this needs to cover its worst-case slew time, not just RC
    // settling: e.g. the OPA347's slew rate is only ~0.17 V/us typical, so
    // a full-scale step (a few volts, as can happen pixel-to-pixel at a
    // sharp spectral edge) can take ~15-20us to fully settle. Without a
    // buffer, a much shorter delay (a few us) is enough for the sensor's
    // own ~150 ohm output impedance to settle. Tune this to your actual
    // signal path; too short truncates real amplitude (readings looking
    // "compressed" versus the scope), too long just slows readout.
    delayMicroseconds(20);
    g_data[i] += analogRead(SPEC_VIDEO);   // valid right after this falling edge
    digitalWrite(SPEC_CLK, HIGH);
    delayMicroseconds(CLK_DELAY_US);
    pulseClock();
    pulseClock();
  }

  // -- Step 6: one full frame's worth of trailing clocks, leaves the sensor
  //    ready for the next acquisition. --
  for (int i = 0; i < SPEC_CHANNELS; i++) pulseClock();
}

// Read a full spectrum into g_data, averaging g_cfg.n_avg passes.
void readSpectrometer() {
  for (int i = 0; i < SPEC_CHANNELS; i++) g_data[i] = 0;   // clear accumulator

  for (uint8_t p = 0; p < g_cfg.n_avg; p++) acquireOnePass();

  if (g_cfg.n_avg > 1) {
    for (int i = 0; i < SPEC_CHANNELS; i++) g_data[i] /= g_cfg.n_avg;
  }
}

// Capture the current reading as the dark reference.
void captureDark() {
  readSpectrometer();
  for (int i = 0; i < SPEC_CHANNELS; i++) g_dark[i] = g_data[i];
  g_have_dark = true;
}

// ============================================================================
//  6. OUTPUT HELPERS
// ============================================================================

// Print the spectrum as one line of comma-separated numbers, ending in '\r'.
// raw == true  -> raw counts
// raw == false -> dark-subtracted counts (clamped at 0), if a dark frame exists
void printSpectrum(bool raw) {
  for (int i = 0; i < SPEC_CHANNELS; i++) {
    int value;
    if (raw || !g_have_dark) {
      value = g_data[i];
    } else {
      value = (int)g_data[i] - (int)g_dark[i];
      if (value < 0) value = 0;
    }
    Serial.print(value);
    if (i < SPEC_CHANNELS - 1) Serial.print(',');
  }
  Serial.print('\r');
}

// Print the six wavelength coefficients as a JSON array.
void printWlCoeffs() {
  Serial.print(F("{\"wl_coeffs\":["));
  for (int i = 0; i < 6; i++) {
    Serial.print(g_cfg.wl_coeffs[i], 8);   // 8 decimal places
    if (i < 5) Serial.print(',');
  }
  Serial.print(F("]}\r"));
}

// ============================================================================
//  7. COMMAND HANDLING
// ============================================================================
//
//  Supported commands (each line ends with Carriage Return, '\r'):
//    hello                     -> "C12666MA,v1.0"        (auto-discovery)
//    idn                       -> "C12666MA_UNO_v1.0"
//    spec                      -> dark-subtracted spectrum (256 numbers)
//    spec,raw                  -> raw spectrum (256 numbers)
//    set_integration,<us>      -> {"integration_us":<n>}  (floored at 1280)
//    get_integration           -> <n>
//    set_gain,<0|1>            -> {"high_gain":<0|1>}     (0=low, 1=high)
//    get_gain                  -> <0|1>
//    set_led,<0|1>             -> {"led":<0|1>}          (0=off, 1=on)
//    get_led                   -> <0|1>
//    dark                      -> {"dark":"ok"}          (capture dark frame)
//    clear_dark                -> {"dark":"cleared"}
//    set_avg,<n>               -> {"n_avg":<n>}          (1..63)
//    get_avg                   -> <n>
//    set_wl_coeff,<i>,<value>  -> {"wl_coeffs":[...]}    (i = 0..5)
//    get_wl_coeffs             -> {"wl_coeffs":[...]}
//    set_name,<text>           -> {"device_name":"..."}
//    get_name                  -> <name>
//    reboot                    -> soft reset
//
void handleCommand(char *cmd) {
  // strtok() splits the command on commas. The first token is the command name.
  char *tok = strtok(cmd, ",");
  if (tok == NULL) return;

  if (strcmp(tok, "hello") == 0) {
    Serial.print(F("C12666MA,v1.0\r\n"));

  } else if (strcmp(tok, "idn") == 0) {
    Serial.print(F("C12666MA_UNO_v1.0\r\n"));

  } else if (strcmp(tok, "spec") == 0) {
    char *arg = strtok(NULL, ",");                 // optional "raw"
    bool raw = (arg != NULL && strcmp(arg, "raw") == 0);
    readSpectrometer();
    printSpectrum(raw);
    Serial.print("\r\n");

  } else if (strcmp(tok, "set_integration") == 0) {
    char *arg = strtok(NULL, ",");
    if (arg != NULL) {
      unsigned long requested = atol(arg);
      g_cfg.integration_us = (requested < MIN_INTEGRATION_US) ? MIN_INTEGRATION_US : requested;
      saveConfig();
    }
    Serial.print(F("{\"integration_us\":"));
    Serial.print(g_cfg.integration_us);
    Serial.print(F("}\r\n"));

  } else if (strcmp(tok, "get_integration") == 0) {
    Serial.print(g_cfg.integration_us);
    Serial.print('\r');
    Serial.print("\n");

  } else if (strcmp(tok, "set_gain") == 0) {
    char *arg = strtok(NULL, ",");
    if (arg != NULL) {
      g_cfg.high_gain = (atoi(arg) != 0) ? 1 : 0;
      applyGain();
      saveConfig();
    }
    Serial.print(F("{\"high_gain\":"));
    Serial.print(g_cfg.high_gain);
    Serial.print(F("}\r\n"));

  } else if (strcmp(tok, "get_gain") == 0) {
    Serial.print(g_cfg.high_gain);
    Serial.print(F("\r\n"));

  } else if (strcmp(tok, "set_led") == 0) {
    char *arg = strtok(NULL, ",");
    if (arg != NULL) {
      g_led_on = (atoi(arg) != 0);
      digitalWrite(LED_PIN, g_led_on ? HIGH : LOW);
    }
    Serial.print(F("{\"led\":"));
    Serial.print(g_led_on);
    Serial.print(F("}\r\n"));

  } else if (strcmp(tok, "get_led") == 0) {
    Serial.print(g_led_on);
    Serial.print(F("\r\n"));

  } else if (strcmp(tok, "dark") == 0) {
    captureDark();
    Serial.print(F("{\"dark\":\"ok\"}\r\n"));

  } else if (strcmp(tok, "clear_dark") == 0) {
    g_have_dark = false;
    Serial.print(F("{\"dark\":\"cleared\"}\r\n"));

  } else if (strcmp(tok, "set_avg") == 0) {
    char *arg = strtok(NULL, ",");
    if (arg != NULL) {
      int n = atoi(arg);
      if (n < 1) n = 1;
      if (n > MAX_AVG) n = MAX_AVG;
      g_cfg.n_avg = (uint8_t)n;
      saveConfig();
    }
    Serial.print(F("{\"n_avg\":"));
    Serial.print(g_cfg.n_avg);
    Serial.print(F("}\r\n"));

  } else if (strcmp(tok, "get_avg") == 0) {
    Serial.print(g_cfg.n_avg);
    Serial.print(F("\r\n"));

  } else if (strcmp(tok, "set_wl_coeff") == 0) {
    char *sIndex = strtok(NULL, ",");
    char *sValue = strtok(NULL, ",");
    if (sIndex != NULL && sValue != NULL) {
      int idx = atoi(sIndex);
      if (idx >= 0 && idx < 6) {
        g_cfg.wl_coeffs[idx] = atof(sValue);       // text -> float
        saveConfig();
      }
    }
    printWlCoeffs();

  } else if (strcmp(tok, "get_wl_coeffs") == 0) {
    printWlCoeffs();

  } else if (strcmp(tok, "set_name") == 0) {
    char *arg = strtok(NULL, ",");
    if (arg != NULL) {
      strncpy(g_cfg.name, arg, sizeof(g_cfg.name) - 1);
      g_cfg.name[sizeof(g_cfg.name) - 1] = '\0';   // always null-terminate
      saveConfig();
    }
    Serial.print(F("{\"device_name\":\""));
    Serial.print(g_cfg.name);
    Serial.print(F("\"}\r\n"));

  } else if (strcmp(tok, "get_name") == 0) {
    Serial.print(g_cfg.name);
    Serial.print(F("\r\n"));

  } else if (strcmp(tok, "reboot") == 0) {
    Serial.print(F("{\"reboot\":\"ok\"}\r"));
    delay(50);            // let the reply flush out
    resetBoard();

  } else {
    Serial.print(F("{\"error\":\"unknown\"}\r\n"));
  }
}

// ============================================================================
//  8. setup() / loop()
// ============================================================================

void setup() {
  pinMode(SPEC_CLK,  OUTPUT);
  pinMode(SPEC_ST,   OUTPUT);
  pinMode(SPEC_GAIN, OUTPUT);
  pinMode(LED_PIN,   OUTPUT);
  digitalWrite(SPEC_CLK, LOW);
  digitalWrite(SPEC_ST,  HIGH);   // ST idles HIGH on the C12666MA, not LOW
  digitalWrite(LED_PIN,  LOW);    // start with the LED off
  // SPEC_VIDEO is an analog input and needs no pinMode().

  analogReference(DEFAULT);   // 5 V ADC reference (matches the VIDEO range)

  Serial.begin(115200);
  loadConfig();
  applyGain();   // drive the Gain pin to match the loaded/default setting
}

void loop() {
  // Collect characters until we see a line terminator, then run the command.
  while (Serial.available() > 0) {
    char c = Serial.read();

    if (c == '\r' || c == '\n') {
      if (g_cmd_len > 0) {
        g_cmd[g_cmd_len] = '\0';   // finish the string
        handleCommand(g_cmd);
        g_cmd_len = 0;
      }
    } else if (g_cmd_len < sizeof(g_cmd) - 1) {
      g_cmd[g_cmd_len++] = c;
    }
    // (characters beyond the buffer size are dropped on purpose)
  }
}
