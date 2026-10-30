/* USER CODE BEGIN Header */
/**
  ******************************************************************************
  * @file           : main.c
  * @brief          : Main program body
  ******************************************************************************
  * @attention
  *
  * Copyright (c) 2026 STMicroelectronics.
  * All rights reserved.
  *
  * This software is licensed under terms that can be found in the LICENSE file
  * in the root directory of this software component.
  * If no LICENSE file comes with this software, it is provided AS-IS.
  *
  ******************************************************************************
  */
/* USER CODE END Header */
/* Includes ------------------------------------------------------------------*/
#include "main.h"

/* Private includes ----------------------------------------------------------*/
/* USER CODE BEGIN Includes */
#include "telemetry_protocol.h"
#include "sensors.h"
#include <stdio.h>
#include <string.h>
/* USER CODE END Includes */

/* Private typedef -----------------------------------------------------------*/
/* USER CODE BEGIN PTD */

/* USER CODE END PTD */

/* Private define ------------------------------------------------------------*/
/* USER CODE BEGIN PD */
#define SENSOR_BRINGUP_ONLY 0

/* Matches fdir/config.py FDIR_TICK_HZ (10 Hz), the rate the published scenario
 * latencies were measured at.  The 1 Hz bring-up rate was too slow for FDIR. */
#define TELEMETRY_PERIOD_MS 100u

/* USER CODE END PD */

/* Private macro -------------------------------------------------------------*/

/* USER CODE BEGIN PM */

/* USER CODE END PM */

/* Private variables ---------------------------------------------------------*/

I2C_HandleTypeDef hi2c1;

UART_HandleTypeDef huart2;

/* USER CODE BEGIN PV */
static ImuSample_t g_imu_device;
static TempSample_t g_temp_device;
static PowerSample_t g_power_device;
static MagnetometerSample_t g_mag_device;
static HAL_StatusTypeDef g_imu_status;
static HAL_StatusTypeDef g_temp_status;
static HAL_StatusTypeDef g_power_status;
static HAL_StatusTypeDef g_mag_status;
#if !SENSOR_BRINGUP_ONLY
static uint16_t g_sequence;
#endif

/* USER CODE END PV */

/* Private function prototypes -----------------------------------------------*/
void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_USART2_UART_Init(void);
static void MX_I2C1_Init(void);
/* USER CODE BEGIN PFP */
static void uart_text(const char *text);
static long scaled(float value, float scale);
static void recover_i2c_bus(void);
static void scan_i2c_bus(void);
static int imu_data_is_live(const ImuSample_t *imu);
#if SENSOR_BRINGUP_ONLY
static void print_sensor_line(const ImuSample_t *imu, HAL_StatusTypeDef imu_status,
                              const MagnetometerSample_t *mag, HAL_StatusTypeDef mag_status,
                              const TempSample_t *temp, HAL_StatusTypeDef temp_status,
                              const PowerSample_t *power, HAL_StatusTypeDef power_status);
#endif

/* USER CODE END PFP */

/* Private user code ---------------------------------------------------------*/
/* USER CODE BEGIN 0 */

/* USER CODE END 0 */

/**
  * @brief  The application entry point.
  * @retval int
  */
int main(void)
{

  /* USER CODE BEGIN 1 */

  /* USER CODE END 1 */

  /* MCU Configuration--------------------------------------------------------*/

  /* Reset of all peripherals, Initializes the Flash interface and the Systick. */
  HAL_Init();

  /* USER CODE BEGIN Init */

  /* USER CODE END Init */

  /* Configure the system clock */
  SystemClock_Config();

  /* USER CODE BEGIN SysInit */

  /* USER CODE END SysInit */

  /* Initialize all configured peripherals */
  MX_GPIO_Init();
  MX_USART2_UART_Init();
  MX_I2C1_Init();
  /* USER CODE BEGIN 2 */
  {
      char msg[96];
      int len;

      uart_text("\r\n=== CubeSAT sensor bring-up ===\r\n");
      recover_i2c_bus();
      scan_i2c_bus();

      g_imu_status = sensors_init_imu(&hi2c1, &g_imu_device);
      if (g_imu_status == HAL_OK) {
          len = sprintf(msg, "IMU  0x%02X WHO=0x%02X OK\r\n",
                        g_imu_device.address, g_imu_device.who_am_i);
          HAL_UART_Transmit(&huart2, (uint8_t *)msg, (uint16_t)len, 100);
      } else {
          uart_text("IMU  FAIL (expected 0x68 or 0x69, WHO_AM_I 0xEA)\r\n");
      }

      g_temp_status = sensors_init_tmp102(&hi2c1, &g_temp_device);
      if (g_temp_status == HAL_OK) {
          len = sprintf(msg, "TEMP 0x%02X ACK\r\n", g_temp_device.address);
          HAL_UART_Transmit(&huart2, (uint8_t *)msg, (uint16_t)len, 100);
      } else {
          uart_text("TEMP FAIL (tried 0x48..0x4B)\r\n");
      }

      g_power_status = sensors_init_ina226(&hi2c1, &g_power_device);
      if (g_power_status == HAL_OK) {
          uint8_t regbuf[2];
          uint16_t cfg_raw = 0, bus_raw = 0, cal_raw = 0;
          len = sprintf(msg, "POWER 0x%02X ACK, shunt_mohm=%ld\r\n",
                        g_power_device.address, scaled(INA226_SHUNT_OHMS, 1000.0f));
          HAL_UART_Transmit(&huart2, (uint8_t *)msg, (uint16_t)len, 100);

          if (HAL_I2C_Mem_Read(&hi2c1, (uint16_t)(g_power_device.address << 1), 0x00,
                                I2C_MEMADD_SIZE_8BIT, regbuf, 2, 100) == HAL_OK) {
              cfg_raw = (uint16_t)((regbuf[0] << 8) | regbuf[1]);
          }
          if (HAL_I2C_Mem_Read(&hi2c1, (uint16_t)(g_power_device.address << 1), 0x02,
                                I2C_MEMADD_SIZE_8BIT, regbuf, 2, 100) == HAL_OK) {
              bus_raw = (uint16_t)((regbuf[0] << 8) | regbuf[1]);
          }
          if (HAL_I2C_Mem_Read(&hi2c1, (uint16_t)(g_power_device.address << 1), 0x05,
                                I2C_MEMADD_SIZE_8BIT, regbuf, 2, 100) == HAL_OK) {
              cal_raw = (uint16_t)((regbuf[0] << 8) | regbuf[1]);
          }
          len = sprintf(msg, "  DEBUG cfg=0x%04X bus_raw=0x%04X cal=0x%04X\r\n",
                        cfg_raw, bus_raw, cal_raw);
          HAL_UART_Transmit(&huart2, (uint8_t *)msg, (uint16_t)len, 100);
      } else {
          uart_text("POWER FAIL (tried 0x40..0x4F)\r\n");
      }

      if (g_imu_status == HAL_OK) {
          g_mag_status = sensors_init_magnetometer(&hi2c1, &g_mag_device);
      } else {
          g_mag_status = HAL_ERROR;
      }
      if (g_mag_status == HAL_OK) {
          len = sprintf(msg, "MAG  0x%02X WIA=0x%02X%02X OK\r\n",
                        g_mag_device.address, g_mag_device.who_am_i_1,
                        g_mag_device.who_am_i_2);
          HAL_UART_Transmit(&huart2, (uint8_t *)msg, (uint16_t)len, 100);
      } else {
          uart_text("MAG  not detected at AK09916 0x0C (IMU accel/gyro still usable)\r\n");
          if (g_imu_status == HAL_OK) {
              len = sprintf(msg, "  DEBUG status=%d WIA=0x%02X%02X (want 0x4809)\r\n",
                            (int)g_mag_status, g_mag_device.who_am_i_1,
                            g_mag_device.who_am_i_2);
              HAL_UART_Transmit(&huart2, (uint8_t *)msg, (uint16_t)len, 100);
              uart_text("  DEBUG with bypass on, ");
              scan_i2c_bus();
              {
                  uint8_t aux_status = 0;
                  uint8_t aux_identity[2] = {0, 0};
                  HAL_StatusTypeDef aux_result = sensors_probe_mag_aux_master(
                      &hi2c1, g_imu_device.address, &aux_status, aux_identity);
                  len = sprintf(msg, "  DEBUG aux master status=%d mst=0x%02X WIA=0x%02X%02X\r\n",
                                (int)aux_result, aux_status, aux_identity[0], aux_identity[1]);
                  HAL_UART_Transmit(&huart2, (uint8_t *)msg, (uint16_t)len, 100);
              }
          }
      }

      uart_text("===============================\r\n");
  }
  /* USER CODE END 2 */

  /* Initialize leds */
  BSP_LED_Init(LED2);

  /* Initialize USER push-button, will be used to trigger an interrupt each time it's pressed.*/
  BSP_PB_Init(BUTTON_USER, BUTTON_MODE_EXTI);

  /* Infinite loop */
  /* USER CODE BEGIN WHILE */
  uint32_t next_tick_ms = HAL_GetTick();
  while (1)
  {

    /* USER CODE END WHILE */

    /* USER CODE BEGIN 3 */
      ImuSample_t imu = {0};
      TempSample_t temp = {0};
      PowerSample_t power = {0};
      MagnetometerSample_t mag = {0};
      HAL_StatusTypeDef imu_read_status = HAL_ERROR;
      HAL_StatusTypeDef temp_read_status = HAL_ERROR;
      HAL_StatusTypeDef power_read_status = HAL_ERROR;
      HAL_StatusTypeDef mag_read_status = HAL_ERROR;

      if (g_imu_status == HAL_OK) {
          imu_read_status = sensors_read_imu(&hi2c1, &g_imu_device, &imu);
          /* An I2C ACK proves the chip answered, not that it is measuring. */
          if (imu_read_status == HAL_OK && !imu_data_is_live(&imu)) {
              imu_read_status = HAL_ERROR;
          }
      }
      if (g_temp_status == HAL_OK) {
          temp_read_status = sensors_read_tmp102(&hi2c1, &g_temp_device, &temp);
      }
      if (g_power_status == HAL_OK) {
          power_read_status = sensors_read_ina226(&hi2c1, &g_power_device, &power);
      }
      if (g_mag_status == HAL_OK) {
          mag_read_status = sensors_read_magnetometer(&hi2c1, &g_mag_device, &mag);
      }

#if SENSOR_BRINGUP_ONLY
      print_sensor_line(&imu, imu_read_status, &mag, mag_read_status,
                        &temp, temp_read_status, &power, power_read_status);
#else
      {
          TelemetryPacket_t pkt = {0};
          uint16_t health_flags = 0;
          uint32_t fault_flags = FAULT_NONE;

          if (temp_read_status == HAL_OK) {
              health_flags |= HEALTH_TEMP_OK;
          } else {
              fault_flags |= FAULT_SENSOR_TIMEOUT;
          }
          if (imu_read_status == HAL_OK) {
              health_flags |= HEALTH_IMU_OK;
          } else {
              fault_flags |= FAULT_SENSOR_TIMEOUT;
          }
          if (mag_read_status == HAL_OK) {
              health_flags |= HEALTH_MAG_OK;
          } else if (g_mag_status == HAL_OK) {
              /* A mag that failed at boot is a known absence, shown by the
               * missing health bit.  Only a read that used to work is a fault. */
              fault_flags |= FAULT_SENSOR_TIMEOUT;
          }
          if (power_read_status == HAL_OK) {
              health_flags |= HEALTH_POWER_OK;
          } else {
              fault_flags |= FAULT_SENSOR_TIMEOUT;
          }

          pkt.seq_num = g_sequence++;
          pkt.timestamp_ms = HAL_GetTick();
          pkt.mode = MODE_NOMINAL;
          pkt.fault_flags = fault_flags;
          pkt.health_flags = health_flags;
          pkt.temp_c = (temp_read_status == HAL_OK) ? temp.temperature_c : 0.0f;
          pkt.accel_x = (imu_read_status == HAL_OK) ? imu.accel_g[0] : 0.0f;
          pkt.accel_y = (imu_read_status == HAL_OK) ? imu.accel_g[1] : 0.0f;
          pkt.accel_z = (imu_read_status == HAL_OK) ? imu.accel_g[2] : 0.0f;
          pkt.gyro_x = (imu_read_status == HAL_OK) ? imu.gyro_dps[0] : 0.0f;
          pkt.gyro_y = (imu_read_status == HAL_OK) ? imu.gyro_dps[1] : 0.0f;
          pkt.gyro_z = (imu_read_status == HAL_OK) ? imu.gyro_dps[2] : 0.0f;
          pkt.mag_x = (mag_read_status == HAL_OK) ? mag.uT[0] : 0.0f;
          pkt.mag_y = (mag_read_status == HAL_OK) ? mag.uT[1] : 0.0f;
          pkt.mag_z = (mag_read_status == HAL_OK) ? mag.uT[2] : 0.0f;
          pkt.bus_voltage_v = (power_read_status == HAL_OK) ? power.bus_voltage_v : 0.0f;
          pkt.bus_current_a = (power_read_status == HAL_OK) ? power.current_a : 0.0f;
          pkt.uptime_s = HAL_GetTick() / 1000u;
          tm_finalize_telemetry(&pkt);
          HAL_UART_Transmit(&huart2, (uint8_t *)&pkt, sizeof(pkt), 100);
      }
#endif

      HAL_GPIO_TogglePin(GPIOA, GPIO_PIN_5);

      /* Schedule against the clock, not a fixed delay, so the time spent on I2C
       * and UART does not stretch the period. */
      next_tick_ms += TELEMETRY_PERIOD_MS;
      if ((int32_t)(HAL_GetTick() - next_tick_ms) >= 0) {
          next_tick_ms = HAL_GetTick();   /* overran: do not burst to catch up */
      }
      while ((int32_t)(HAL_GetTick() - next_tick_ms) < 0) {
      }
  }
  /* USER CODE END 3 */
}

/**
  * @brief System Clock Configuration
  * @retval None
  */
void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};

  /** Configure the main internal regulator output voltage
  */
  __HAL_RCC_PWR_CLK_ENABLE();
  __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE3);

  /** Initializes the RCC Oscillators according to the specified parameters
  * in the RCC_OscInitTypeDef structure.
  */
  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSI;
  RCC_OscInitStruct.HSIState = RCC_HSI_ON;
  RCC_OscInitStruct.HSICalibrationValue = RCC_HSICALIBRATION_DEFAULT;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSI;
  RCC_OscInitStruct.PLL.PLLM = 16;
  RCC_OscInitStruct.PLL.PLLN = 336;
  RCC_OscInitStruct.PLL.PLLP = RCC_PLLP_DIV4;
  RCC_OscInitStruct.PLL.PLLQ = 2;
  RCC_OscInitStruct.PLL.PLLR = 2;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK)
  {
    Error_Handler();
  }

  /** Initializes the CPU, AHB and APB buses clocks
  */
  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK|RCC_CLOCKTYPE_SYSCLK
                              |RCC_CLOCKTYPE_PCLK1|RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV2;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;

  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_2) != HAL_OK)
  {
    Error_Handler();
  }
}

/**
  * @brief I2C1 Initialization Function
  * @param None
  * @retval None
  */
static void MX_I2C1_Init(void)
{

  /* USER CODE BEGIN I2C1_Init 0 */

  /* USER CODE END I2C1_Init 0 */

  /* USER CODE BEGIN I2C1_Init 1 */

  /* USER CODE END I2C1_Init 1 */
  hi2c1.Instance = I2C1;
  hi2c1.Init.ClockSpeed = 100000;
  hi2c1.Init.DutyCycle = I2C_DUTYCYCLE_2;
  hi2c1.Init.OwnAddress1 = 0;
  hi2c1.Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;
  hi2c1.Init.DualAddressMode = I2C_DUALADDRESS_DISABLE;
  hi2c1.Init.OwnAddress2 = 0;
  hi2c1.Init.GeneralCallMode = I2C_GENERALCALL_DISABLE;
  hi2c1.Init.NoStretchMode = I2C_NOSTRETCH_DISABLE;
  if (HAL_I2C_Init(&hi2c1) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN I2C1_Init 2 */

  /* USER CODE END I2C1_Init 2 */

}

/**
  * @brief USART2 Initialization Function
  * @param None
  * @retval None
  */
static void MX_USART2_UART_Init(void)
{

  /* USER CODE BEGIN USART2_Init 0 */

  /* USER CODE END USART2_Init 0 */

  /* USER CODE BEGIN USART2_Init 1 */

  /* USER CODE END USART2_Init 1 */
  huart2.Instance = USART2;
  huart2.Init.BaudRate = 115200;
  huart2.Init.WordLength = UART_WORDLENGTH_8B;
  huart2.Init.StopBits = UART_STOPBITS_1;
  huart2.Init.Parity = UART_PARITY_NONE;
  huart2.Init.Mode = UART_MODE_TX_RX;
  huart2.Init.HwFlowCtl = UART_HWCONTROL_NONE;
  huart2.Init.OverSampling = UART_OVERSAMPLING_16;
  if (HAL_UART_Init(&huart2) != HAL_OK)
  {
    Error_Handler();
  }
  /* USER CODE BEGIN USART2_Init 2 */

  /* USER CODE END USART2_Init 2 */

}

/**
  * @brief GPIO Initialization Function
  * @param None
  * @retval None
  */
static void MX_GPIO_Init(void)
{
  /* USER CODE BEGIN MX_GPIO_Init_1 */

  /* USER CODE END MX_GPIO_Init_1 */

  /* GPIO Ports Clock Enable */
  __HAL_RCC_GPIOC_CLK_ENABLE();
  __HAL_RCC_GPIOH_CLK_ENABLE();
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_GPIOB_CLK_ENABLE();

  /* USER CODE BEGIN MX_GPIO_Init_2 */

  /* USER CODE END MX_GPIO_Init_2 */
}

/* USER CODE BEGIN 4 */

static void uart_text(const char *text)
{
    HAL_UART_Transmit(&huart2, (uint8_t *)text, (uint16_t)strlen(text), 100);
}

static long scaled(float value, float scale)
{
    return (long)(value * scale);
}

/*
 * A reset can land in the middle of a read while a sensor is driving SDA low.
 * The sensor is not reset with the MCU, so it keeps waiting for clocks and the
 * whole bus looks dead until power is cycled.  Report the idle line levels,
 * then clock the sensor out of that state and send a STOP.
 */
static void recover_i2c_bus(void)
{
    GPIO_InitTypeDef pins = {0};
    GPIO_PinState scl_before;
    GPIO_PinState sda_before;
    char msg[64];
    int len;

    HAL_I2C_DeInit(&hi2c1);

    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8 | GPIO_PIN_9, GPIO_PIN_SET);
    pins.Pin = GPIO_PIN_8 | GPIO_PIN_9;
    pins.Mode = GPIO_MODE_OUTPUT_OD;
    pins.Pull = GPIO_PULLUP;
    pins.Speed = GPIO_SPEED_FREQ_LOW;
    HAL_GPIO_Init(GPIOB, &pins);
    HAL_Delay(1);

    scl_before = HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_8);
    sda_before = HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_9);

    for (uint32_t pulse = 0; pulse < 9u &&
         HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_9) == GPIO_PIN_RESET; ++pulse) {
        HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8, GPIO_PIN_RESET);
        HAL_Delay(1);
        HAL_GPIO_WritePin(GPIOB, GPIO_PIN_8, GPIO_PIN_SET);
        HAL_Delay(1);
    }

    /* STOP: SDA rises while SCL is high. */
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_9, GPIO_PIN_RESET);
    HAL_Delay(1);
    HAL_GPIO_WritePin(GPIOB, GPIO_PIN_9, GPIO_PIN_SET);
    HAL_Delay(1);

    len = sprintf(msg, "I2C lines SCL=%d SDA=%d, after recovery SCL=%d SDA=%d\r\n",
                  (int)scl_before, (int)sda_before,
                  (int)HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_8),
                  (int)HAL_GPIO_ReadPin(GPIOB, GPIO_PIN_9));
    HAL_UART_Transmit(&huart2, (uint8_t *)msg, (uint16_t)len, 100);

    /* Init returns the pins to I2C and resets the peripheral's BUSY state. */
    if (HAL_I2C_Init(&hi2c1) != HAL_OK) {
        Error_Handler();
    }
}

static void scan_i2c_bus(void)
{
    char msg[32];
    uint8_t found = 0;

    uart_text("I2C ACK addresses:");
    for (uint8_t address = 1; address < 0x7Fu; ++address) {
        if (HAL_I2C_IsDeviceReady(&hi2c1, (uint16_t)(address << 1), 1, 5) == HAL_OK) {
            int len = sprintf(msg, " 0x%02X", address);
            HAL_UART_Transmit(&huart2, (uint8_t *)msg, (uint16_t)len, 100);
            found = 1;
        }
    }
    uart_text(found ? "\r\n" : " NONE\r\n");
}

/*
 * Diagnostics use integer-scaled values because newlib-nano normally omits
 * printf's floating-point formatter.  Units are included in the field names:
 * temperature is centi-degrees C, acceleration is milli-g, angular rate is
 * centi-degrees/s, magnetic field is centi-uT, voltage is mV, and current is
 * mA.  The binary telemetry path below still carries the original floats.
 */
#if SENSOR_BRINGUP_ONLY
static void print_sensor_line(const ImuSample_t *imu, HAL_StatusTypeDef imu_status,
                              const MagnetometerSample_t *mag, HAL_StatusTypeDef mag_status,
                              const TempSample_t *temp, HAL_StatusTypeDef temp_status,
                              const PowerSample_t *power, HAL_StatusTypeDef power_status)
{
    char msg[256];
    int len = sprintf(msg,
                      "DATA imu=%s ax_mg=%ld ay_mg=%ld az_mg=%ld "
                      "gx_cdps=%ld gy_cdps=%ld gz_cdps=%ld imu_temp_cC=%ld "
                      "mag=%s mx_cuT=%ld my_cuT=%ld mz_cuT=%ld "
                      "temp_cC=%ld bus_mV=%ld current_mA=%ld power_mW=%ld\r\n",
                      imu_status == HAL_OK ? "OK" : "FAIL",
                      scaled(imu->accel_g[0], 1000.0f),
                      scaled(imu->accel_g[1], 1000.0f),
                      scaled(imu->accel_g[2], 1000.0f),
                      scaled(imu->gyro_dps[0], 100.0f),
                      scaled(imu->gyro_dps[1], 100.0f),
                      scaled(imu->gyro_dps[2], 100.0f),
                      scaled(imu->temperature_c, 100.0f),
                      mag_status == HAL_OK ? "OK" : "FAIL",
                      scaled(mag->uT[0], 100.0f),
                      scaled(mag->uT[1], 100.0f),
                      scaled(mag->uT[2], 100.0f),
                      temp_status == HAL_OK ? scaled(temp->temperature_c, 100.0f) : 0L,
                      power_status == HAL_OK ? scaled(power->bus_voltage_v, 1000.0f) : 0L,
                      power_status == HAL_OK ? scaled(power->current_a, 1000.0f) : 0L,
                      power_status == HAL_OK ? scaled(power->power_w, 1000.0f) : 0L);
    HAL_UART_Transmit(&huart2, (uint8_t *)msg, (uint16_t)len, 100);
}
#endif

/*
 * A sleeping ICM-20948 still ACKs and returns zeros in every data register.
 * Real noise makes all seven channels exactly zero at once effectively
 * impossible, so this tells "asleep" from "measuring" without a threshold.
 */
static int imu_data_is_live(const ImuSample_t *imu)
{
    for (int axis = 0; axis < 3; axis++) {
        if (imu->accel_raw[axis] != 0 || imu->gyro_raw[axis] != 0) {
            return 1;
        }
    }
    return imu->temperature_raw != 0;
}

/* USER CODE END 4 */

/**
  * @brief  This function is executed in case of error occurrence.
  * @retval None
  */
void Error_Handler(void)
{
  /* USER CODE BEGIN Error_Handler_Debug */
  /* User can add his own implementation to report the HAL error return state */
  __disable_irq();
  while (1)
  {
  }
  /* USER CODE END Error_Handler_Debug */
}
#ifdef USE_FULL_ASSERT
/**
  * @brief  Reports the name of the source file and the source line number
  *         where the assert_param error has occurred.
  * @param  file: pointer to the source file name
  * @param  line: assert_param error line source number
  * @retval None
  */
void assert_failed(uint8_t *file, uint32_t line)
{
  /* USER CODE BEGIN 6 */
  /* User can add his own implementation to report the file name and line number,
     ex: printf("Wrong parameters value: file %s on line %d\r\n", file, line) */
  /* USER CODE END 6 */
}
#endif /* USE_FULL_ASSERT */
