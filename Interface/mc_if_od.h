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
 *  Type convention:
 *   - CiA-402 standard objects (0x1xxx, 0x6xxx): scaled integers, factors below.
 *   - Manufacturer objects (0x2xxx): FLOAT32 in SI units (rad, rad/s, A, Nm, V) -- exact,
 *     no scaling -- which is ideal for gain tuning and live graphing.
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
#define MC_IF_MODE_PROFILE_VELOCITY  (3)
#define MC_IF_MODE_TORQUE            (4)
#define MC_IF_MODE_JOYSTICK_VELOCITY (-1)
#define MC_IF_MODE_HOMING            (6)

/* ===== Calibration commands (0x2700/1) ===== */
#define MC_IF_CAL_NONE             (0u)
#define MC_IF_CAL_ALIGN_CAPTURE    (1u)
#define MC_IF_CAL_CURRENT_OFFSET   (2u)

/* ===== Persistence (0x2800) ===== */
#define MC_IF_SAVE_MAGIC           (0x7376u)  /* write to 0x2800/1 to request a save */
#define MC_IF_FACTORY_RESET_MAGIC  (0x7274u)  /* write to 0x2800/3 to request factory reset */

/* ===== Test-injection targets (0x2900/2) ===== */
#define MC_IF_INJECT_NONE          (0u)
#define MC_IF_INJECT_IQ            (1u)
#define MC_IF_INJECT_VELOCITY      (2u)
#define MC_IF_INJECT_POSITION      (3u)

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

/**
 * @brief Canonical OD object list.  X(index, subindex, name, type, access, flags)
 *
 * Manufacturer 0x2xxx values are FLOAT32 SI. Standard 0x1xxx/0x6xxx are scaled ints.
 * Expand on each side to build the table; keep both ends generated from this list.
 */
#define MC_IF_OD_OBJECTS(X) \
    /* --- CiA-402 core --- */ \
    X(0x1000, 0, device_type,                 MC_IF_T_U32, MC_IF_A_RO, MC_IF_F_NONE) \
    X(0x1001, 0, error_register,              MC_IF_T_U8,  MC_IF_A_RO, MC_IF_F_NONE) \
    X(0x603F, 0, error_code,                  MC_IF_T_U16, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x6040, 0, controlword,                 MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_PDO) \
    X(0x6041, 0, statusword,                  MC_IF_T_U16, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x6060, 0, modes_of_operation,          MC_IF_T_I8,  MC_IF_A_RW, MC_IF_F_PDO) \
    X(0x6061, 0, modes_of_operation_display,  MC_IF_T_I8,  MC_IF_A_RO, MC_IF_F_PDO) \
    /* --- Motion targets/actuals (scaled int) --- */ \
    X(0x607A, 0, target_position,             MC_IF_T_I32, MC_IF_A_RW, MC_IF_F_PDO) \
    X(0x6064, 0, position_actual,             MC_IF_T_I32, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x6081, 0, profile_velocity,            MC_IF_T_U32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x6083, 0, profile_acceleration,        MC_IF_T_U32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x6084, 0, profile_deceleration,        MC_IF_T_U32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x6085, 0, quick_stop_deceleration,     MC_IF_T_U32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x60FF, 0, target_velocity,             MC_IF_T_I32, MC_IF_A_RW, MC_IF_F_PDO) \
    X(0x606C, 0, velocity_actual,             MC_IF_T_I32, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x6071, 0, target_torque,               MC_IF_T_I32, MC_IF_A_RW, MC_IF_F_PDO) \
    X(0x6077, 0, torque_actual,               MC_IF_T_I32, MC_IF_A_RO, MC_IF_F_PDO) \
    /* --- 0x2000 axis / motor model (float SI) --- */ \
    X(0x2000, 1, motor_kt_nm_per_a,           MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2000, 2, motor_inertia_kg_m2,         MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2000, 3, motor_resistance_ohm,        MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2000, 4, motor_inductance_h,          MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2000, 5, motor_pole_pairs,            MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_PERSIST) \
    /* --- 0x2200 position controller --- */ \
    X(0x2200, 1, pos_kp,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2200, 2, pos_ki,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2200, 3, pos_kd,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    /* --- 0x2300 velocity controller + telemetry --- */ \
    X(0x2300, 1, vel_kp,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2300, 2, vel_ki,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2300, 3, vel_kd,                      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2300, 4, vel_current_limit_a,         MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2310, 1, tlm_vel_demand_rad_s,        MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x2310, 2, tlm_vel_actual_rad_s,        MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x2310, 3, tlm_vel_iq_cmd_a,            MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO) \
    /* --- 0x2400 current/FOC gains + telemetry --- */ \
    X(0x2400, 1, foc_id_kp,                   MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2400, 2, foc_id_ki,                   MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2400, 3, foc_iq_kp,                   MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2400, 4, foc_iq_ki,                   MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2400, 5, foc_voltage_limit_v,         MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2410, 1, tlm_id_meas_a,               MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x2410, 2, tlm_iq_meas_a,               MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x2410, 3, tlm_vd_v,                    MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x2410, 4, tlm_vq_v,                    MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x2410, 5, tlm_electrical_angle_rad,    MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO) \
    /* --- 0x2500 encoder / state estimator + telemetry --- */ \
    X(0x2500, 1, est_electrical_offset_rad,   MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2500, 2, est_velocity_filter_hz,      MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2500, 3, est_obs_kp,                  MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2500, 4, est_obs_ki,                  MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2500, 5, est_obs_kv,                  MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2500, 6, est_use_observer,            MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2510, 1, tlm_mech_position_rad,       MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x2510, 2, tlm_mech_velocity_rad_s,     MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO) \
    /* --- 0x2600 faults / limits / diagnostics --- */ \
    X(0x2600, 1, fault_flags,                 MC_IF_T_U32, MC_IF_A_RO, MC_IF_F_PDO) \
    X(0x2600, 2, current_trip_a,              MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_PERSIST) \
    X(0x2600, 3, tlm_bus_voltage_v,           MC_IF_T_F32, MC_IF_A_RO, MC_IF_F_PDO) \
    /* --- 0x2700 calibration --- */ \
    X(0x2700, 1, cal_command,                 MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_NONE) \
    X(0x2700, 2, cal_status,                  MC_IF_T_U16, MC_IF_A_RO, MC_IF_F_NONE) \
    /* --- 0x2800 persistent store --- */ \
    X(0x2800, 1, store_save_command,          MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_NONE) \
    X(0x2800, 2, store_status,                MC_IF_T_U16, MC_IF_A_RO, MC_IF_F_NONE) \
    X(0x2800, 3, store_factory_reset,         MC_IF_T_U16, MC_IF_A_RW, MC_IF_F_NONE) \
    /* --- 0x2900 commissioning / test injection (step changes for tuning) --- */ \
    X(0x2900, 1, inject_enable,               MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE) \
    X(0x2900, 2, inject_target,               MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE) \
    X(0x2900, 3, inject_step_amplitude,       MC_IF_T_F32, MC_IF_A_RW, MC_IF_F_NONE) \
    X(0x2900, 4, inject_step_trigger,         MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE) \
    /* --- 0x2A00 telemetry map: sub0 = count; sub1..16 are U32 map entries (array) --- */ \
    X(0x2A00, 0, tlm_map_count,               MC_IF_T_U8,  MC_IF_A_RW, MC_IF_F_NONE)

#endif /* MC_IF_OD_H */
