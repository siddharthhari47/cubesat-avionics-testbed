/*
 * Sensor bring-up drivers for the CubeSAT NUCLEO-F446RE testbed.
 *
 * These functions deliberately stop at physical measurements.  They do not
 * decide FDIR state; the caller owns the health/fault policy.
 */

#ifndef CUBESAT_SENSORS_H
#define CUBESAT_SENSORS_H

#include "stm32f4xx_hal.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* ICM-20948 defaults to +/-2 g and +/-250 dps after reset. */
#define ICM20948_WHO_AM_I_VALUE       0xEAu
#define ICM20948_ACCEL_LSB_PER_G      16384.0f
#define ICM20948_GYRO_LSB_PER_DPS     131.0f
#define ICM20948_TEMP_LSB_PER_C       333.87f
#define ICM20948_TEMP_OFFSET_C        21.0f

/* TMP102's normal-mode temperature resolution is 0.0625 degrees C/LSB. */
#define TMP102_TEMP_LSB_C             0.0625f

/*
 * Change this to the value printed on the INA226 breakout's shunt resistor.
 * 0.1 ohm is common on low-cost INA226 modules, but it is not universal.
 */
#ifndef INA226_SHUNT_OHMS
#define INA226_SHUNT_OHMS              0.1f
#endif

/* The selected current LSB gives 0.1 mA/count with a 0.1 ohm shunt. */
#define INA226_CURRENT_LSB_A            0.0001f

typedef struct {
    uint8_t present;
    uint8_t address;
    uint8_t who_am_i;
    int16_t accel_raw[3];
    int16_t gyro_raw[3];
    int16_t temperature_raw;
    float accel_g[3];
    float gyro_dps[3];
    float temperature_c;
} ImuSample_t;

typedef struct {
    uint8_t present;
    uint8_t address;
    int16_t raw;
    float temperature_c;
} TempSample_t;

typedef struct {
    uint8_t present;
    uint8_t address;
    int16_t shunt_raw;
    uint16_t bus_raw;
    int16_t current_raw;
    uint16_t power_raw;
    float shunt_voltage_v;
    float bus_voltage_v;
    float current_a;
    float power_w;
} PowerSample_t;

typedef struct {
    uint8_t present;
    uint8_t address;
    uint8_t who_am_i_1;
    uint8_t who_am_i_2;
    int16_t raw[3];
    float uT[3];
} MagnetometerSample_t;

/* Probe and initialize one device.  The address fields are filled on success. */
HAL_StatusTypeDef sensors_init_imu(I2C_HandleTypeDef *hi2c, ImuSample_t *sample);
HAL_StatusTypeDef sensors_read_imu(I2C_HandleTypeDef *hi2c, const ImuSample_t *device,
                                   ImuSample_t *sample);

HAL_StatusTypeDef sensors_init_tmp102(I2C_HandleTypeDef *hi2c, TempSample_t *sample);
HAL_StatusTypeDef sensors_read_tmp102(I2C_HandleTypeDef *hi2c, const TempSample_t *device,
                                      TempSample_t *sample);

HAL_StatusTypeDef sensors_init_ina226(I2C_HandleTypeDef *hi2c, PowerSample_t *sample);
HAL_StatusTypeDef sensors_read_ina226(I2C_HandleTypeDef *hi2c, const PowerSample_t *device,
                                      PowerSample_t *sample);

/* Optional AK09916 magnetometer embedded in an ICM-20948 module. */
HAL_StatusTypeDef sensors_init_magnetometer(I2C_HandleTypeDef *hi2c,
                                            MagnetometerSample_t *sample);
HAL_StatusTypeDef sensors_read_magnetometer(I2C_HandleTypeDef *hi2c,
                                            const MagnetometerSample_t *device,
                                            MagnetometerSample_t *sample);

/* Bring-up diagnostic: read the AK09916 ID without using bypass mode. */
HAL_StatusTypeDef sensors_probe_mag_aux_master(I2C_HandleTypeDef *hi2c, uint8_t icm_address,
                                               uint8_t *mst_status, uint8_t identity[2]);

#ifdef __cplusplus
}
#endif

#endif /* CUBESAT_SENSORS_H */
