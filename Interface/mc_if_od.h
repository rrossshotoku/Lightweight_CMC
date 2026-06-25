#ifndef MC_IF_OD_H
#define MC_IF_OD_H
#include <stdint.h>

/** @file mc_if_od.h
 *  @brief SHARED inter-MCU boundary contract: object dictionary (index map + scaling).
 *
 *  Single source of truth for the OD layout exposed by the motor-control MCU over SPI and,
 *  in turn, on the network MCU's external (Ethernet) interface. Both firmwares and host tools
 *  include this file. The canonical object list is the X-macro MC_IF_OD_OBJECTS(X) at the end:
 *  each side expands it to build its own table / generate code, so the two never drift.
 *
 *  Each entry carries an OWNER column (MC_IfOdOwner_t). The owner determines which firmware
 *  actually handles reads and writes for that entry:
 *    - MC_IF_OWNER_MOTOR : motor MCU's OD table handles it. Reads/writes from the network
 *                          MCU travel over SPI via the cia402 OD pipeline. Encompasses every
 *                          CiA-402 standard (0x1xxx/0x6xxx) entry and every motor-MCU
 *                          manufacturer entry (0x2xxx).
 *    - MC_IF_OWNER_CMC   : the network MCU (Lightweight_CMC) handles it locally. These entries
 *                          back the CMC's axis_manager and are the universal command surface
 *                          that all protocol modules (camerad, visca, PC tool, ...) speak to.
 *                          No SPI traffic is generated when these are read/written.
 *
 *  Type convention:
 *   - CiA-402 standard objects (0x1xxx, 0x6xxx): scaled integers, factors below.
 *   - Manufacturer objects (0x2xxx): FLOAT32 in SI units (rad, rad/s, A, Nm, V) -- exact,
 *     no scaling -- which is ideal for gain tuning and live graphing.
 *   - CMC-owned axis_manager objects (0x3xxx): FLOAT32 SI for analog values, U8 for modes /
 *     state / triggers. Same SI convention as 0x2xxx so all of axis_manager's public surface
 *     can be entered and read in physical units.
 */

/** @brief OD data types (values match the motor MCU's MC_OdType_t). */
typedef enum
{
    MC_IF_T_U8 = 0,
    MC_IF_T_U16,
    MC_IF_T_U32,
    MC_IF_T_I8,
    MC_IF_T_I16,
    MC_IF_T_I32,
    MC_IF_T_F32
} MC_IfOdType_t;

/** @brief OD access rights. */
typedef enum
{
    MC_IF_A_RO = 1,
    MC_IF_A_WO = 2,
    MC_IF_A_RW = 3
} MC_IfOdAccess_t;

/** @brief OD entry owner -- which firmware handles reads/writes for the entry.
 *
 *  Added in MC_IF_PROTOCOL_VERSION 2. Both firmwares filter MC_IF_OD_OBJECTS(X) by owner
 *  when building their local OD table; host tools (e.g. PC GUI) iterate everything and
 *  display the owner alongside the entry.
 */
typedef enum
{
    MC_IF_OWNER_MOTOR = 0,   /* handled by Generic_motor_controller's OD table */
    MC_IF_OWNER_CMC   = 1    /* handled by Lightweight_CMC's app/od/cmc_od */
} MC_IfOdOwner_t;

/** @brief OD entry flags (bitmask). */
#define MC_IF_F_NONE     (0x00u)
#define MC_IF_F_PDO      (0x01u)   /* mappable into the cyclic process data */
#define MC_IF_F_PERSIST  (0x02u)   /* saved by the persistent store */

/* ===== Scaling for CiA-402 standard (scaled-int) objects =====
 * SI value = raw * scale ;  raw = round(SI / scale). */
#define MC_IF_POS_SCALE   (1.0e-5f)  /* rad per LSB   (I32: +-21474 rad ~ +-3418 rev, 10 urad) */
#define MC_IF_VEL_SCALE   (1.0e-3f)  /* rad/s per LSB (1 mrad/s) */
#define MC_IF_ACC_SCALE   (1.0e-3f)  /* rad/s^2 per LSB */
#define MC_IF_CUR_SCALE   (1.0e-3f)  /* A per LSB (1 mA) -- target/actual "torque/current" */
#define MC_IF_TRQ_SCALE   (1.0e-3f)  /* Nm per LSB (1 mNm) if used as torque */

/* ===== Controlword (0x6040) bits ===== */
#define MC_IF_CW_ENABLE        (0x0001u)  /* enable operation */
#define MC_IF_CW_QUICK_STOP    (0x0002u)  /* 0 = quick stop active */
#define MC_IF_CW_NEW_SETPOINT  (0x0010u)  /* rising edge: execute currently-configured move
                                           * (PROFILE_POSITION / PROFILE_VELOCITY / TORQUE etc.
                                           *  All setup parameters MUST have been written via
                                           *  SDO before this rising edge.) */
#define MC_IF_CW_FAULT_RESET   (0x0080u)  /* rising edge clears latched faults */
#define MC_IF_CW_HALT          (0x0100u)  /* controlled stop, hold position */

/* ===== Statusword (0x6041) bits ===== */
#define MC_IF_SW_READY          (0x0001u)
#define MC_IF_SW_ENABLED        (0x0004u)
#define MC_IF_SW_FAULT          (0x0008u)
#define MC_IF_SW_TARGET_REACHED (0x0400u)
#define MC_IF_SW_LIMIT_ACTIVE   (0x0800u)

/* ===== Modes of operation (0x6060) ===== */
#define MC_IF_MODE_DISABLED          (0)
#define MC_IF_MODE_PROFILE_POSITION  (1)
#define MC_IF_MODE_PROFILE_VELOCITY  (3)   /* live velocity from cyclic velocity_setpoint */
#define MC_IF_MODE_TORQUE            (4)
#define MC_IF_MODE_HOMING            (6)
/* Note: a separate "joystick" mode used to live here (-1); v3 removed it.
 * The motor MCU has no application-specific joystick concept. The CMC's
 * axis_manager translates joystick_value × joystick_max_velocity locally
 * and streams the result as velocity_setpoint in PROFILE_VELOCITY mode. */

/* ===== Calibration commands (0x2700/1) ===== */
#define MC_IF_CAL_NONE             (0u)
#define MC_IF_CAL_ALIGN_CAPTURE    (1u)
#define MC_IF_CAL_CURRENT_OFFSET   (2u)   /* measure phase-current ADC offsets; power stage must be off (ADR-026) */
#define MC_IF_CAL_SET_MECH_ZERO    (3u)   /* capture current position as the mechanical home (ADR-022) */

/* ===== Calibration completeness (0x2700/5 cal_done_flags, RO bitfield) =====
 * A set bit means that calibration currently has valid data; a CLEAR bit means it is
 * outstanding (not yet done). Lets a tool show what still needs calibrating. (ADR-026) */
#define MC_IF_CAL_DONE_ELECTRICAL      (0x0001u)  /* electrical-angle offset captured (alignment, 0x2700/1=1) */
#define MC_IF_CAL_DONE_MECH_ZERO       (0x0002u)  /* mechanical home set (0x2700/1=3)                          */
#define MC_IF_CAL_DONE_CURRENT_OFFSET  (0x0004u)  /* phase-current ADC offsets measured (0x2700/1=2)           */

/* ===== Persistence (0x2800) ===== */
#define MC_IF_SAVE_MAGIC           (0x7376u)  /* write to 0x2800/1 to request a save */
#define MC_IF_FACTORY_RESET_MAGIC  (0x7274u)  /* write to 0x2800/3 to request factory reset */

/* store_status (0x2800/2, RO) bitfield */
#define MC_IF_STORE_VALID    (0x0001u)  /* a valid saved record exists in flash */
#define MC_IF_STORE_PENDING  (0x0002u)  /* a save is latched, awaiting power-stage-off to commit -- flash
                                         * can't be written while the drive is live; disable it to commit */

/* ===== Test-injection targets (0x2900/2) ===== */
#define MC_IF_INJECT_NONE          (0u)
#define MC_IF_INJECT_IQ            (1u)
#define MC_IF_INJECT_VELOCITY      (2u)
#define MC_IF_INJECT_POSITION      (3u)

/* ===== Loop-tuning test modes (0x2910/1 test_mode) ===== (ADR-030)
 * Motor-owned commissioning overlay: while the matching operational mode is enabled, an on-motor
 * signal generator drives that loop's reference for PID tuning. NOT a CiA-402 mode. */
#define MC_IF_TEST_MODE_OFF        (0u)   /* normal operation                                              */
#define MC_IF_TEST_MODE_VELOCITY   (1u)   /* generator -> velocity-loop demand (needs PROFILE_VELOCITY)    */
#define MC_IF_TEST_MODE_POSITION   (2u)   /* generator -> position demand, bypass trajectory (PROFILE_POSITION) */

/* ===== Telemetry (TX-PDO) map: 0x2A00 =====
 * Host-configurable list of OD entries streamed in the cyclic telemetry frame. The map is an
 * OD array: sub0 = count, sub1..MC_IF_TLM_MAX_ENTRIES = U32 map entries (RW). It is
 * RUNTIME-reconfigurable over the live link; see INTERFACE_SPEC.md "Telemetry mapping". */
#define MC_IF_TLM_MAP_INDEX     (0x2A00u)
#define MC_IF_TLM_MAX_ENTRIES   (16u)     /* 0x2A00:1 .. 0x2A00:16 */
#define MC_IF_TLM_MAX_BYTES     (40u)     /* mapped-blob budget in a 64-byte frame */

/* Map entry (U32) = (index<<16) | (subindex<<8) | bitlen, bitlen in bits (8/16/32). */
#define MC_IF_TLM_MAP_ENTRY(index, sub, bits) \
    (((uint32_t)(index) << 16) | ((uint32_t)(sub) << 8) | (uint32_t)(bits))
#define MC_IF_TLM_MAP_INDEX_OF(e) ((uint16_t)((e) >> 16))
#define MC_IF_TLM_MAP_SUB_OF(e)   ((uint8_t)((e) >> 8))
#define MC_IF_TLM_MAP_BITS_OF(e)  ((uint8_t)(e))

/* ===== axis_manager (CMC-owned) modes for op_mode (0x3020) =====
 * High-level operational modes the CMC's axis_manager exposes to controllers.
 * Distinct from CiA-402 modes_of_operation (0x6060) — axis_manager translates
 * these into the appropriate motor MCU mode + cyclic targets. */
#define MC_IF_AXIS_MODE_OFF                (0u)   /* no commands sent; motor stays disabled */
#define MC_IF_AXIS_MODE_JOYSTICK           (1u)   /* axis follows joystick_value * joystick_max_velocity */
#define MC_IF_AXIS_MODE_PROFILE_VELOCITY   (2u)   /* axis follows target_velocity (rad/s) */
#define MC_IF_AXIS_MODE_PROFILE_POSITION   (3u)   /* axis moves to target_position over target_time */
#define MC_IF_AXIS_MODE_HOLD               (4u)   /* axis holds its current position */
#define MC_IF_AXIS_MODE_TORQUE             (5u)   /* axis follows axis_target_current [A]; motor 0x6060 = MC_IF_MODE_TORQUE (REQ-0012) */

/* ===== axis_manager (CMC-owned) state values for axis_state (0x3000) ===== */
#define MC_IF_AXIS_STATE_DISABLED  (0u)
#define MC_IF_AXIS_STATE_READY     (1u)
#define MC_IF_AXIS_STATE_RUNNING   (2u)
#define MC_IF_AXIS_STATE_FAULT     (3u)

/**
 * @brief Canonical OD object list.  X(index, subindex, name, type, access, flags, owner)
 *
 * Manufacturer 0x2xxx values are FLOAT32 SI. Standard 0x1xxx/0x6xxx are scaled ints.
 * 0x3xxx are CMC-owned axis_manager entries (FLOAT32 SI / U8 modes & state).
 * Expand on each side to build the table; keep both ends generated from this list.
 *
 * Each side filters by owner:
 *   - motor MCU generates its OD table from entries with owner == MC_IF_OWNER_MOTOR
 *   - CMC      generates its OD table from entries with owner == MC_IF_OWNER_CMC
 *   - host tools iterate everything; the owner is shown alongside the entry
 */
#define MC_IF_OD_OBJECTS(X) \
    /* --- CiA-402 core --- */ \
    X(0x1000, 0, device_type,                 MC_IF_T_U32, MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x1001, 0, error_register,              MC_IF_T_U8,  MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x603F, 0, error_code,                  MC_IF_T_U16, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x6040, 0, controlword,                 MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x6041, 0, statusword,                  MC_IF_T_U16, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x6060, 0, modes_of_operation,          MC_IF_T_I8,  MC_IF_A_RW, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x6061, 0, modes_of_operation_display,  MC_IF_T_I8,  MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    /* --- Motion targets/actuals (scaled int).
     * NOTE (v3): target_position / target_velocity / target_torque are no
     * longer carried in MC_IfCyclicCommand_t. They are now SDO-only writes
     * that the motor MCU stores until a move is triggered via the
     * controlword NEW_SETPOINT bit. The MC_IF_F_PDO flag here means
     * "PDO-mappable into the configurable telemetry blob (0x2A00)" -- you
     * can still graph commanded targets vs actuals from the host side. */ \
    X(0x607A, 0, target_position,             MC_IF_T_I32, MC_IF_A_RW, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x607B, 0, target_position_time_ms,     MC_IF_T_U32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x6064, 0, position_actual,             MC_IF_T_I32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x6081, 0, profile_velocity,            MC_IF_T_U32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x6083, 0, profile_acceleration,        MC_IF_T_U32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x6084, 0, profile_deceleration,        MC_IF_T_U32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x6085, 0, quick_stop_deceleration,     MC_IF_T_U32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x60FF, 0, target_velocity,             MC_IF_T_I32, MC_IF_A_RW, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x606C, 0, velocity_actual,             MC_IF_T_I32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x6071, 0, target_torque,               MC_IF_T_I32, MC_IF_A_RW, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x6077, 0, torque_actual,               MC_IF_T_I32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    /* --- 0x2000 axis / motor model (float SI) --- */ \
    X(0x2000, 1, motor_kt_nm_per_a,           MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2000, 2, motor_inertia_kg_m2,         MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2000, 3, motor_resistance_ohm,        MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2000, 4, motor_inductance_h,          MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2000, 5, motor_pole_pairs,            MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    /* --- 0x2200 position controller --- */ \
    X(0x2200, 1, pos_kp,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2200, 2, pos_ki,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2200, 3, pos_kd,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2200, 4, velocity_ff_gain,            MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    /* --- 0x2300 velocity controller + telemetry --- */ \
    X(0x2300, 1, vel_kp,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2300, 2, vel_ki,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2300, 3, vel_kd,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2300, 4, vel_current_limit_a,         MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2300, 5, vel_load_factor,             MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2310, 1, tlm_vel_demand_rad_s,        MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x2310, 2, tlm_vel_actual_rad_s,        MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x2310, 3, tlm_vel_iq_cmd_a,            MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    /* --- 0x2400 current/FOC gains + telemetry --- */ \
    X(0x2400, 1, foc_id_kp,                   MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2400, 2, foc_id_ki,                   MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2400, 3, foc_iq_kp,                   MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2400, 4, foc_iq_ki,                   MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2400, 5, foc_voltage_limit_v,         MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2410, 1, tlm_id_meas_a,               MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x2410, 2, tlm_iq_meas_a,               MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x2410, 3, tlm_vd_v,                    MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x2410, 4, tlm_vq_v,                    MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x2410, 5, tlm_electrical_angle_rad,    MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    /* --- 0x2500 encoder / state estimator + telemetry --- */ \
    X(0x2500, 1, est_electrical_offset_rad,   MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2500, 2, est_velocity_filter_hz,      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2500, 3, est_obs_kp,                  MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2500, 4, est_obs_ki,                  MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2500, 5, est_obs_kv,                  MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2500, 6, est_use_observer,            MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2510, 1, tlm_mech_position_rad,       MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x2510, 2, tlm_mech_velocity_rad_s,     MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x2510, 3, tlm_pos_demand_rad,          MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    /* --- 0x2600 faults / limits / diagnostics --- */ \
    X(0x2600, 1, fault_flags,                 MC_IF_T_U32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x2600, 2, current_trip_a,              MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2600, 3, tlm_bus_voltage_v,           MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    /* --- 0x2700 calibration --- */ \
    X(0x2700, 1, cal_command,                 MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2700, 2, cal_status,                  MC_IF_T_U16, MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2700, 3, cal_align_current_a,         MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2700, 4, cal_align_hold_ms,           MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_MOTOR) \
    X(0x2700, 5, cal_done_flags,              MC_IF_T_U16, MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    /* --- 0x2800 persistent store --- */ \
    X(0x2800, 1, store_save_command,          MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2800, 2, store_status,                MC_IF_T_U16, MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2800, 3, store_factory_reset,         MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    /* --- 0x2900 commissioning / test injection (step changes for tuning) --- */ \
    X(0x2900, 1, inject_enable,               MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2900, 2, inject_target,               MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2900, 3, inject_step_amplitude,       MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2900, 4, inject_step_trigger,         MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    /* --- 0x2910 loop-tuning test-signal overlay (ADR-030; amplitude/rate units follow test_mode) --- */ \
    X(0x2910, 1, test_mode,                   MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2910, 2, test_amplitude,              MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2910, 3, test_rate,                   MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2910, 4, test_dwell_s,                MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2910, 5, test_continuous,             MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2910, 6, test_trigger,                MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2910, 7, test_active,                 MC_IF_T_U8,  MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2910, 8, test_signal,                 MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO,     MC_IF_OWNER_MOTOR) \
    X(0x2910, 9, test_pause_s,                MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    X(0x2910, 10, test_max_accel,             MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    /* --- 0x2A00 telemetry map: sub0 = count; sub1..16 are U32 map entries (array) --- */ \
    X(0x2A00, 0, tlm_map_count,               MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_MOTOR) \
    /* === CMC-owned axis_manager entries (axis 0). Reserve 0x3100-0x31FF for axis 1, etc. === */ \
    /* --- 0x3000-0x300F state (RO) --- */ \
    X(0x3000, 0, axis_state,                  MC_IF_T_U8,  MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3001, 0, axis_op_mode_actual,         MC_IF_T_U8,  MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3002, 0, axis_position_actual,        MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3003, 0, axis_velocity_actual,        MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3004, 0, axis_error_code,             MC_IF_T_U16, MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3005, 0, axis_error_register,         MC_IF_T_U8,  MC_IF_A_RO, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    /* --- 0x3010-0x301F commands (write-triggered) --- */ \
    X(0x3010, 0, axis_enable,                 MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3011, 0, axis_quick_stop,             MC_IF_T_U8,  MC_IF_A_WO, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3012, 0, axis_clear_fault,            MC_IF_T_U8,  MC_IF_A_WO, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3013, 0, axis_start_move,             MC_IF_T_U8,  MC_IF_A_WO, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    /* --- 0x3020-0x302F mode + per-mode targets --- */ \
    X(0x3020, 0, axis_op_mode,                MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3021, 0, axis_joystick_value,         MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3022, 0, axis_joystick_max_velocity,  MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_CMC) \
    X(0x3023, 0, axis_target_velocity,        MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3024, 0, axis_target_position,        MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3025, 0, axis_target_time,            MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    /* --- 0x3026-0x302A joystick calibration (raw -> normalised, symmetric output) --- */ \
    /* Protocol modules write the raw stick value to axis_joystick_raw; axis_manager     */ \
    /* normalises using the four cal entries below and updates axis_joystick_value.      */ \
    /* For sources that have an already-normalised value, write axis_joystick_value      */ \
    /* directly; that bypasses the raw-side cal.                                         */ \
    X(0x3026, 0, axis_joystick_raw,           MC_IF_T_I32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3027, 0, axis_joystick_raw_center,    MC_IF_T_I32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_CMC) \
    X(0x3028, 0, axis_joystick_raw_full_pos,  MC_IF_T_I32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_CMC) \
    X(0x3029, 0, axis_joystick_raw_full_neg,  MC_IF_T_I32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_CMC) \
    X(0x302A, 0, axis_joystick_raw_deadband,  MC_IF_T_U32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_CMC) \
    /* Operator current command (REQ-0012). Effective only in AXIS_MODE_TORQUE.        */ \
    /* axis_manager SDO-writes target_torque (0x6071) = round(current_a / CUR_SCALE).  */ \
    X(0x302B, 0, axis_target_current,         MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    /* On-board UP/DOWN button current magnitude [A]. While AXIS_MODE_TORQUE is active */ \
    /* and a button is held, axis_manager overrides axis_target_current to             */ \
    /* +button_current (UP) or -button_current (DOWN). Released -> 0. Default 0 A.     */ \
    X(0x302C, 0, axis_button_current,         MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    /* --- 0x3030-0x303F limits --- */ \
    X(0x3030, 0, axis_velocity_limit,         MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_CMC) \
    X(0x3031, 0, axis_position_limit_lo,      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_CMC) \
    X(0x3032, 0, axis_position_limit_hi,      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_CMC) \
    X(0x3033, 0, axis_accel_limit,            MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST, MC_IF_OWNER_CMC) \
    /* --- 0x3040-0x304F dynamics / payload reserved (kept free for any     */ \
    /* future CMC-side dynamics state that doesn't belong on the motor).    */ \
    /* The earlier CMC-only axis_payload_weight_kg (0x3040) was REMOVED     */ \
    /* in CHANGELOG [4.1.0] — the operator-tunable load multiplier moved    */ \
    /* to motor-owned 0x2300:5 vel_load_factor (REQ-0014) so it actually    */ \
    /* scales the velocity loop's kp/ki at runtime.                          */ \
    /* --- 0x3050-0x305F CMC persistence triggers --- */ \
    /* Write MC_IF_SAVE_MAGIC (0x7376) to commit the corresponding region   */ \
    /* to the CMC's internal flash. Same magic constant used by the motor   */ \
    /* MCU's 0x2800:1 save_command; this is the CMC-side equivalent.        */ \
    X(0x3050, 0, cmc_save_config,             MC_IF_T_U16, MC_IF_A_WO, MC_IF_F_NONE,    MC_IF_OWNER_CMC) \
    X(0x3051, 0, cmc_save_shots,              MC_IF_T_U16, MC_IF_A_WO, MC_IF_F_NONE,    MC_IF_OWNER_CMC)

#endif /* MC_IF_OD_H */
