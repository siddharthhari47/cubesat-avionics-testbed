/* Sensor drivers for the CubeSAT NUCLEO-F446RE bring-up firmware. */

#include "sensors.h"

#include <string.h>

/* 7-bit I2C addresses. HAL expects these shifted left by one. */
#define ICM20948_ADDR_LOW              0x68u
#define ICM20948_ADDR_HIGH             0x69u
#define AK09916_ADDR                   0x0Cu

#define TMP102_ADDR_MIN                0x48u
#define TMP102_ADDR_MAX                0x4Bu

#define INA226_ADDR_MIN                0x40u
#define INA226_ADDR_MAX                0x4Fu

/* ICM-20948 user bank 0 registers. */
#define ICM_REG_WHO_AM_I               0x00u
#define ICM_REG_USER_CTRL              0x03u
#define ICM_REG_PWR_MGMT_1             0x06u
#define ICM_REG_PWR_MGMT_2             0x07u
#define ICM_REG_LP_CONFIG               0x05u
#define ICM_REG_INT_PIN_CFG             0x0Fu
#define ICM_REG_I2C_MST_STATUS          0x17u
#define ICM_REG_ACCEL_XOUT_H           0x2Du
#define ICM_REG_TEMP_OUT_H             0x39u
#define ICM_REG_BANK_SEL                0x7Fu

/* ICM-20948 user bank 2 registers. */
#define ICM_REG_GYRO_CONFIG_1           0x01u
#define ICM_REG_ACCEL_CONFIG             0x14u

/* ICM-20948 user bank 3 registers: the auxiliary I2C master. */
#define ICM_REG_I2C_MST_CTRL            0x01u
#define ICM_REG_I2C_SLV4_ADDR           0x13u
#define ICM_REG_I2C_SLV4_REG            0x14u
#define ICM_REG_I2C_SLV4_CTRL           0x15u
#define ICM_REG_I2C_SLV4_DI             0x17u

#define ICM_I2C_MST_STATUS_SLV4_DONE    0x40u
#define ICM_I2C_MST_STATUS_SLV4_NACK    0x10u

/* AK09916 magnetometer registers. */
#define AK_REG_WIA1                     0x00u
#define AK_REG_WIA2                     0x01u
#define AK_REG_ST1                      0x10u
#define AK_REG_HXL                      0x11u
#define AK_REG_CNTL2                    0x31u
#define AK_REG_ST2                      0x18u

/* TMP102 registers. */
#define TMP_REG_TEMPERATURE             0x00u
#define TMP_REG_CONFIGURATION           0x01u

/* INA226 registers. */
#define INA_REG_CONFIGURATION           0x00u
#define INA_REG_SHUNT_VOLTAGE           0x01u
#define INA_REG_BUS_VOLTAGE             0x02u
#define INA_REG_POWER                   0x03u
#define INA_REG_CURRENT                 0x04u
#define INA_REG_CALIBRATION             0x05u
#define INA_REG_MANUFACTURER_ID         0xFEu
#define INA226_MANUFACTURER_ID_VALUE    0x5449u  /* ASCII "TI" */

static HAL_StatusTypeDef read_bytes(I2C_HandleTypeDef *hi2c, uint8_t address,
                                     uint8_t reg, uint8_t *data, uint16_t length)
{
    return HAL_I2C_Mem_Read(hi2c, (uint16_t)(address << 1), reg,
                            I2C_MEMADD_SIZE_8BIT, data, length, 100);
}

static HAL_StatusTypeDef write_bytes(I2C_HandleTypeDef *hi2c, uint8_t address,
                                      uint8_t reg, const uint8_t *data, uint16_t length)
{
    return HAL_I2C_Mem_Write(hi2c, (uint16_t)(address << 1), reg,
                             I2C_MEMADD_SIZE_8BIT, (uint8_t *)data, length, 100);
}

static HAL_StatusTypeDef read_u16_be(I2C_HandleTypeDef *hi2c, uint8_t address,
                                     uint8_t reg, uint16_t *value)
{
    uint8_t data[2];
    HAL_StatusTypeDef status = read_bytes(hi2c, address, reg, data, sizeof(data));
    if (status == HAL_OK) {
        *value = (uint16_t)(((uint16_t)data[0] << 8) | data[1]);
    }
    return status;
}

static HAL_StatusTypeDef write_u16_be(I2C_HandleTypeDef *hi2c, uint8_t address,
                                      uint8_t reg, uint16_t value)
{
    const uint8_t data[2] = {
        (uint8_t)(value >> 8),
        (uint8_t)(value & 0xFFu),
    };
    return write_bytes(hi2c, address, reg, data, sizeof(data));
}

static HAL_StatusTypeDef select_icm_bank_at(I2C_HandleTypeDef *hi2c, uint8_t address,
                                             uint8_t bank)
{
    const uint8_t value = (uint8_t)((bank & 0x03u) << 4);
    return write_bytes(hi2c, address, ICM_REG_BANK_SEL, &value, 1);
}

static HAL_StatusTypeDef icm_write_at(I2C_HandleTypeDef *hi2c, uint8_t address,
                                      uint8_t reg, const uint8_t *data, uint16_t length)
{
    return write_bytes(hi2c, address, reg, data, length);
}

static int16_t be_i16(const uint8_t *data)
{
    return (int16_t)(((uint16_t)data[0] << 8) | data[1]);
}

HAL_StatusTypeDef sensors_init_imu(I2C_HandleTypeDef *hi2c, ImuSample_t *sample)
{
    const uint8_t addresses[] = {ICM20948_ADDR_LOW, ICM20948_ADDR_HIGH};
    uint8_t who_am_i = 0;
    HAL_StatusTypeDef status = HAL_ERROR;

    memset(sample, 0, sizeof(*sample));

    for (uint32_t i = 0; i < sizeof(addresses); ++i) {
        status = read_bytes(hi2c, addresses[i], ICM_REG_WHO_AM_I, &who_am_i, 1);
        if (status == HAL_OK && who_am_i == ICM20948_WHO_AM_I_VALUE) {
            sample->present = 1;
            sample->address = addresses[i];
            sample->who_am_i = who_am_i;
            break;
        }
    }
    if (!sample->present) {
        return status;
    }

    status = select_icm_bank_at(hi2c, sample->address, 0);
    if (status != HAL_OK) {
        return status;
    }

    /* Reset, select the best available clock, and wake both motion engines. */
    {
        const uint8_t reset = 0x80u;
        const uint8_t clock = 0x01u;
        const uint8_t enable = 0x00u;
        status = icm_write_at(hi2c, sample->address, ICM_REG_PWR_MGMT_1, &reset, 1);
        if (status != HAL_OK) {
            return status;
        }
        HAL_Delay(100);
        status = icm_write_at(hi2c, sample->address, ICM_REG_PWR_MGMT_1, &clock, 1);
        if (status == HAL_OK) {
            status = icm_write_at(hi2c, sample->address, ICM_REG_PWR_MGMT_2, &enable, 1);
        }
        if (status != HAL_OK) {
            return status;
        }
    }

    /* Bank 2: FS_SEL=0 keeps +/-250 dps and +/-2 g; enable the DLPF path. */
    status = select_icm_bank_at(hi2c, sample->address, 2);
    if (status == HAL_OK) {
        const uint8_t default_range_and_filter = 0x01u;
        status = icm_write_at(hi2c, sample->address, ICM_REG_GYRO_CONFIG_1,
                              &default_range_and_filter, 1);
        if (status == HAL_OK) {
            status = icm_write_at(hi2c, sample->address, ICM_REG_ACCEL_CONFIG,
                                  &default_range_and_filter, 1);
        }
    }
    if (status != HAL_OK) {
        return status;
    }

    return select_icm_bank_at(hi2c, sample->address, 0);
}

HAL_StatusTypeDef sensors_read_imu(I2C_HandleTypeDef *hi2c, const ImuSample_t *device,
                                   ImuSample_t *sample)
{
    uint8_t data[14];
    HAL_StatusTypeDef status;

    *sample = *device;
    if (!device->present) {
        return HAL_ERROR;
    }

    status = select_icm_bank_at(hi2c, device->address, 0);
    if (status != HAL_OK) {
        return status;
    }
    status = read_bytes(hi2c, device->address, ICM_REG_ACCEL_XOUT_H, data, sizeof(data));
    if (status != HAL_OK) {
        return status;
    }

    /* Burst layout from 0x2D: accel bytes 0-5, gyro 6-11, temperature 12-13. */
    for (uint32_t axis = 0; axis < 3; ++axis) {
        sample->accel_raw[axis] = be_i16(&data[axis * 2]);
        sample->gyro_raw[axis] = be_i16(&data[6 + axis * 2]);
        sample->accel_g[axis] = (float)sample->accel_raw[axis] / ICM20948_ACCEL_LSB_PER_G;
        sample->gyro_dps[axis] = (float)sample->gyro_raw[axis] / ICM20948_GYRO_LSB_PER_DPS;
    }
    sample->temperature_raw = be_i16(&data[12]);
    sample->temperature_c = (float)sample->temperature_raw / ICM20948_TEMP_LSB_PER_C +
                            ICM20948_TEMP_OFFSET_C;
    return HAL_OK;
}

HAL_StatusTypeDef sensors_init_tmp102(I2C_HandleTypeDef *hi2c, TempSample_t *sample)
{
    HAL_StatusTypeDef status = HAL_ERROR;
    memset(sample, 0, sizeof(*sample));

    for (uint8_t address = TMP102_ADDR_MIN; address <= TMP102_ADDR_MAX; ++address) {
        uint8_t config[2];
        status = read_bytes(hi2c, address, TMP_REG_CONFIGURATION, config, sizeof(config));
        if (status == HAL_OK) {
            sample->present = 1;
            sample->address = address;
            break;
        }
    }
    return sample->present ? HAL_OK : status;
}

HAL_StatusTypeDef sensors_read_tmp102(I2C_HandleTypeDef *hi2c, const TempSample_t *device,
                                      TempSample_t *sample)
{
    uint8_t data[2];
    uint16_t raw_register;
    int16_t raw;
    HAL_StatusTypeDef status;

    *sample = *device;
    if (!device->present) {
        return HAL_ERROR;
    }
    status = read_bytes(hi2c, device->address, TMP_REG_TEMPERATURE, data, sizeof(data));
    if (status != HAL_OK) {
        return status;
    }

    raw_register = (uint16_t)(((uint16_t)data[0] << 8) | data[1]);
    /* EM=0 is the reset/default mode; EM=1 uses one extra temperature bit. */
    if ((data[1] & 0x01u) != 0u) {
        raw = (int16_t)(raw_register & 0xFFF8u);
        raw = (int16_t)(raw >> 3);
    } else {
        raw = (int16_t)(raw_register & 0xFFF0u);
        raw = (int16_t)(raw >> 4);
    }
    sample->raw = raw;
    sample->temperature_c = (float)raw * TMP102_TEMP_LSB_C;
    return HAL_OK;
}

HAL_StatusTypeDef sensors_init_ina226(I2C_HandleTypeDef *hi2c, PowerSample_t *sample)
{
    HAL_StatusTypeDef status = HAL_ERROR;
    memset(sample, 0, sizeof(*sample));

    for (uint8_t address = INA226_ADDR_MIN; address <= INA226_ADDR_MAX; ++address) {
        uint16_t manufacturer_id;
        status = read_u16_be(hi2c, address, INA_REG_MANUFACTURER_ID, &manufacturer_id);
        if (status == HAL_OK && manufacturer_id == INA226_MANUFACTURER_ID_VALUE) {
            sample->present = 1;
            sample->address = address;
            break;
        }
    }
    if (!sample->present) {
        return status;
    }

    /* Continuous shunt+bus conversion, default conversion times/averaging. */
    status = write_u16_be(hi2c, sample->address, INA_REG_CONFIGURATION, 0x4127u);
    if (status != HAL_OK) {
        return status;
    }

    /* Cal = 0.00512 / (Current_LSB * Rshunt). */
    {
        const float calibration_float = 0.00512f /
                                         (INA226_CURRENT_LSB_A * INA226_SHUNT_OHMS);
        uint16_t calibration;
        if (calibration_float < 1.0f || calibration_float > 65535.0f) {
            return HAL_ERROR;
        }
        calibration = (uint16_t)calibration_float;
        status = write_u16_be(hi2c, sample->address, INA_REG_CALIBRATION, calibration);
    }
    return status;
}

HAL_StatusTypeDef sensors_read_ina226(I2C_HandleTypeDef *hi2c, const PowerSample_t *device,
                                      PowerSample_t *sample)
{
    uint16_t raw;
    HAL_StatusTypeDef status;

    *sample = *device;
    if (!device->present) {
        return HAL_ERROR;
    }

    status = read_u16_be(hi2c, device->address, INA_REG_SHUNT_VOLTAGE, &raw);
    if (status != HAL_OK) {
        return status;
    }
    sample->shunt_raw = (int16_t)raw;
    sample->shunt_voltage_v = (float)sample->shunt_raw * 0.0000025f;

    status = read_u16_be(hi2c, device->address, INA_REG_BUS_VOLTAGE, &sample->bus_raw);
    if (status != HAL_OK) {
        return status;
    }
    sample->bus_voltage_v = (float)sample->bus_raw * 0.00125f;

    status = read_u16_be(hi2c, device->address, INA_REG_CURRENT, &raw);
    if (status != HAL_OK) {
        return status;
    }
    sample->current_raw = (int16_t)raw;
    sample->current_a = (float)sample->current_raw * INA226_CURRENT_LSB_A;

    status = read_u16_be(hi2c, device->address, INA_REG_POWER, &sample->power_raw);
    if (status != HAL_OK) {
        return status;
    }
    sample->power_w = (float)sample->power_raw * (25.0f * INA226_CURRENT_LSB_A);
    return HAL_OK;
}

HAL_StatusTypeDef sensors_init_magnetometer(I2C_HandleTypeDef *hi2c,
                                            MagnetometerSample_t *sample)
{
    uint8_t identity[2];
    uint8_t icm_address = ICM20948_ADDR_LOW;
    HAL_StatusTypeDef status;
    memset(sample, 0, sizeof(*sample));

    /* Match the ICM address selected by its AD0 pin instead of assuming 0x68. */
    {
        const uint8_t addresses[] = {ICM20948_ADDR_LOW, ICM20948_ADDR_HIGH};
        uint8_t who_am_i = 0;
        status = HAL_ERROR;
        for (uint32_t i = 0; i < sizeof(addresses); ++i) {
            status = read_bytes(hi2c, addresses[i], ICM_REG_WHO_AM_I, &who_am_i, 1);
            if (status == HAL_OK && who_am_i == ICM20948_WHO_AM_I_VALUE) {
                icm_address = addresses[i];
                break;
            }
        }
        if (who_am_i != ICM20948_WHO_AM_I_VALUE) {
            return status == HAL_OK ? HAL_ERROR : status;
        }
    }

    /* Put the ICM's auxiliary I2C bus into host-bypass mode. */
    status = select_icm_bank_at(hi2c, icm_address, 0);
    if (status != HAL_OK) {
        return status;
    }
    {
        const uint8_t aux_master_reset = 0x02u;
        const uint8_t user_ctrl_without_aux_master = 0x00u;
        const uint8_t bypass_enable = 0x02u;
        /* Clear any half-finished aux transaction; it can hold the aux bus. */
        status = icm_write_at(hi2c, icm_address, ICM_REG_USER_CTRL, &aux_master_reset, 1);
        if (status == HAL_OK) {
            HAL_Delay(10);
            status = icm_write_at(hi2c, icm_address, ICM_REG_USER_CTRL,
                                  &user_ctrl_without_aux_master, 1);
        }
        if (status == HAL_OK) {
            status = icm_write_at(hi2c, icm_address, ICM_REG_INT_PIN_CFG,
                                  &bypass_enable, 1);
        }
    }
    if (status != HAL_OK) {
        return status;
    }
    HAL_Delay(10);

    status = read_bytes(hi2c, AK09916_ADDR, AK_REG_WIA1, identity, sizeof(identity));
    if (status != HAL_OK) {
        return status;
    }
    /* Keep the identity even on a mismatch so the boot report can show it. */
    sample->who_am_i_1 = identity[0];
    sample->who_am_i_2 = identity[1];
    if (identity[0] != 0x48u || identity[1] != 0x09u) {
        return HAL_ERROR;
    }

    sample->present = 1;
    sample->address = AK09916_ADDR;
    return HAL_OK;
}

static HAL_StatusTypeDef icm_write_u8(I2C_HandleTypeDef *hi2c, uint8_t address,
                                      uint8_t reg, uint8_t value)
{
    return write_bytes(hi2c, address, reg, &value, 1);
}

/* One AK09916 register read, performed by the ICM on its own auxiliary bus. */
static HAL_StatusTypeDef aux_master_read(I2C_HandleTypeDef *hi2c, uint8_t icm_address,
                                         uint8_t reg, uint8_t *value,
                                         uint8_t *mst_status)
{
    HAL_StatusTypeDef status = select_icm_bank_at(hi2c, icm_address, 3);
    if (status == HAL_OK) {
        status = icm_write_u8(hi2c, icm_address, ICM_REG_I2C_SLV4_ADDR,
                              (uint8_t)(0x80u | AK09916_ADDR));
    }
    if (status == HAL_OK) {
        status = icm_write_u8(hi2c, icm_address, ICM_REG_I2C_SLV4_REG, reg);
    }
    if (status == HAL_OK) {
        status = icm_write_u8(hi2c, icm_address, ICM_REG_I2C_SLV4_CTRL, 0x80u);
    }
    if (status == HAL_OK) {
        status = select_icm_bank_at(hi2c, icm_address, 0);
    }
    if (status != HAL_OK) {
        return status;
    }

    /* The status register clears when read, so keep every bit that was seen. */
    *mst_status = 0;
    for (uint32_t attempt = 0; attempt < 500u; ++attempt) {
        uint8_t latest = 0;
        status = read_bytes(hi2c, icm_address, ICM_REG_I2C_MST_STATUS, &latest, 1);
        if (status != HAL_OK) {
            return status;
        }
        *mst_status |= latest;
        if ((latest & ICM_I2C_MST_STATUS_SLV4_DONE) != 0u) {
            break;
        }
        HAL_Delay(2);
    }
    if ((*mst_status & ICM_I2C_MST_STATUS_SLV4_DONE) == 0u) {
        return HAL_TIMEOUT;
    }
    if ((*mst_status & ICM_I2C_MST_STATUS_SLV4_NACK) != 0u) {
        return HAL_ERROR;
    }

    status = select_icm_bank_at(hi2c, icm_address, 3);
    if (status == HAL_OK) {
        status = read_bytes(hi2c, icm_address, ICM_REG_I2C_SLV4_DI, value, 1);
    }
    return status;
}

HAL_StatusTypeDef sensors_probe_mag_aux_master(I2C_HandleTypeDef *hi2c, uint8_t icm_address,
                                               uint8_t *mst_status, uint8_t identity[2])
{
    HAL_StatusTypeDef result;
    HAL_StatusTypeDef restore;

    result = select_icm_bank_at(hi2c, icm_address, 0);
    if (result == HAL_OK) {
        result = icm_write_u8(hi2c, icm_address, ICM_REG_LP_CONFIG, 0x00u);
    }
    if (result == HAL_OK) {
        result = icm_write_u8(hi2c, icm_address, ICM_REG_INT_PIN_CFG, 0x00u);
    }
    if (result == HAL_OK) {
        result = icm_write_u8(hi2c, icm_address, ICM_REG_USER_CTRL, 0x20u);
    }
    if (result == HAL_OK) {
        result = select_icm_bank_at(hi2c, icm_address, 3);
    }
    if (result == HAL_OK) {
        /* Clock setting 7 is 345.6 kHz; bit 4 sends a STOP between reads. */
        result = icm_write_u8(hi2c, icm_address, ICM_REG_I2C_MST_CTRL, 0x17u);
    }
    if (result == HAL_OK) {
        HAL_Delay(10);
        result = aux_master_read(hi2c, icm_address, AK_REG_WIA1, &identity[0], mst_status);
    }
    if (result == HAL_OK) {
        result = aux_master_read(hi2c, icm_address, AK_REG_WIA2, &identity[1], mst_status);
    }

    /* Leave the ICM as sensors_init_magnetometer left it, in bank 0. */
    restore = select_icm_bank_at(hi2c, icm_address, 0);
    if (restore == HAL_OK) {
        restore = icm_write_u8(hi2c, icm_address, ICM_REG_USER_CTRL, 0x02u);
    }
    if (restore == HAL_OK) {
        HAL_Delay(10);
        restore = icm_write_u8(hi2c, icm_address, ICM_REG_USER_CTRL, 0x00u);
    }
    if (restore == HAL_OK) {
        restore = icm_write_u8(hi2c, icm_address, ICM_REG_INT_PIN_CFG, 0x02u);
    }
    return result != HAL_OK ? result : restore;
}

HAL_StatusTypeDef sensors_read_magnetometer(I2C_HandleTypeDef *hi2c,
                                            const MagnetometerSample_t *device,
                                            MagnetometerSample_t *sample)
{
    uint8_t status_byte;
    uint8_t data[6];
    uint8_t dummy_status;
    HAL_StatusTypeDef status;

    *sample = *device;
    if (!device->present) {
        return HAL_ERROR;
    }

    /* Single-measurement mode avoids leaving the bus in a continuous mode. */
    {
        const uint8_t mode = 0x01u;
        status = write_bytes(hi2c, device->address, AK_REG_CNTL2, &mode, 1);
    }
    if (status != HAL_OK) {
        return status;
    }
    HAL_Delay(10);

    status = read_bytes(hi2c, device->address, AK_REG_ST1, &status_byte, 1);
    if (status != HAL_OK || (status_byte & 0x01u) == 0u) {
        return status == HAL_OK ? HAL_BUSY : status;
    }
    status = read_bytes(hi2c, device->address, AK_REG_HXL, data, sizeof(data));
    if (status != HAL_OK) {
        return status;
    }
    /* AK09916 magnetometer output is little-endian: low byte first. */
    for (uint32_t axis = 0; axis < 3; ++axis) {
        sample->raw[axis] = (int16_t)(((uint16_t)data[axis * 2 + 1] << 8) |
                                      data[axis * 2]);
        sample->uT[axis] = (float)sample->raw[axis] * 0.15f;
    }

    /* Reading ST2 is required to release the AK09916 data registers. */
    return read_bytes(hi2c, device->address, AK_REG_ST2, &dummy_status, 1);
}
