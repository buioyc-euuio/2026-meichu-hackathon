// micro:bit 端程式：貼到 MakeCode 的「JavaScript」分頁，再切回「積木」就會變成積木
// 需要「藍牙 bluetooth」擴充（不要同時裝 V7RC 擴充，兩個都會搶藍牙訊息）

bluetooth.startUartService()
basic.showIcon(IconNames.Heart)          // 開機：愛心

bluetooth.onBluetoothConnected(function () {
    basic.showIcon(IconNames.Yes)        // 連上：打勾
})

bluetooth.onBluetoothDisconnected(function () {
    basic.showIcon(IconNames.No)         // 斷線：打叉
})

// 每收到一筆以 # 結尾的訊息就執行一次
bluetooth.onUartDataReceived(serial.delimiters(Delimiters.Hash), function () {
    let msg = bluetooth.uartReadUntil(serial.delimiters(Delimiters.Hash))
    if (msg == "U") {
        basic.showArrow(ArrowNames.North)
    } else if (msg == "D") {
        basic.showArrow(ArrowNames.South)
    } else if (msg == "L") {
        basic.showArrow(ArrowNames.West)
    } else if (msg == "R") {
        basic.showArrow(ArrowNames.East)
    } else if (msg == "S") {
        basic.clearScreen()
    }
})
