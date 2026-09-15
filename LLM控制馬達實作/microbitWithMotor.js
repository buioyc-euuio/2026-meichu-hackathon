/**
 * micro:bit 端範例：配合 robot_bluetooth.py 的指令
 * 
 * 貼到 MakeCode「JavaScript」分頁再切回積木；需要「藍牙 bluetooth」擴充
 * 
 * 把每個 if 裡的「顯示箭頭」換成你的馬達積木即可
 * 
 * F = 前進   B = 後退   L = 左轉   R = 右轉   S = 停止
 */
function left_wheel (turn: number, speed: number) {
    sensors.DDMmotor(
    AnalogPin.P0,
    (left_wheel_forward + turn + 1) % 2,
    AnalogPin.P16,
    speed
    )
}
function servo (angle: number) {
    pins.servoWritePin(AnalogPin.P8, angle)
}
bluetooth.onBluetoothConnected(function () {
    basic.showIcon(IconNames.Yes)
})
// TODO：加上「馬達停止」，斷線時一定要停車，避免車子一直往前衝
bluetooth.onBluetoothDisconnected(function () {
    basic.showIcon(IconNames.No)
})
input.onButtonPressed(Button.A, function () {
    servo(180)
})
/**
 * 三個控制馬達的函式：
 * 
 * left_wheel, right_wheel, camara_z
 * 
 * 統一變數
 * 
 * turn=1就是往前走或是往上升
 * 
 * turn=0就是往後走或是往下降
 * 
 * -----
 * 
 * servo 最右邊是0 最左邊是180
 */
function camara_z (turn: number, speed: number) {
    sensors.DDMmotor(
    AnalogPin.P15,
    (camara_z_up + turn + 1) % 2,
    AnalogPin.P14,
    speed
    )
}
function right_wheel (turn: number, speed: number) {
    sensors.DDMmotor(
    AnalogPin.P2,
    (right_wheel_forward + turn + 1) % 2,
    AnalogPin.P1,
    speed
    )
}
bluetooth.onUartDataReceived(serial.delimiters(Delimiters.Hash), function () {
    cmd = bluetooth.uartReadUntil(serial.delimiters(Delimiters.Hash))
    if (cmd == "F") {
        // TODO：換成「馬達前進」
        led.plot(2, 0)
    } else if (cmd == "B") {
        // TODO：換成「馬達後退」
        led.plot(2, 4)
    } else if (cmd == "L") {
        // TODO：換成「馬達左轉」
        led.plot(0, 2)
    } else if (cmd == "R") {
        // TODO：換成「馬達右轉」
        led.plot(4, 2)
    } else if (cmd == "S") {
        // TODO：換成「馬達停止」
        basic.clearScreen()
    }
})
input.onButtonPressed(Button.AB, function () {
    servo(90)
})
input.onButtonPressed(Button.B, function () {
    servo(0)
})
let cmd = ""
let camara_z_up = 0
let right_wheel_forward = 0
let left_wheel_forward = 0
bluetooth.startUartService()
basic.showIcon(IconNames.Heart)
left_wheel_forward = 0
right_wheel_forward = 1
camara_z_up = 0
