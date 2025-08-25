// 1. 引入 lottie-web 函式庫
import lottie from "https://cdn.jsdelivr.net/npm/lottie-web@5.12.2/build/player/lottie_light.min.js";
// 2. 引入你的 JSON 動畫檔案
import animationData from "./lottieflow-attention-8-000000-easey.json" assert { type: "json" };

// 3. 在頁面載入完成後，執行 Lottie 的初始化
window.addEventListener("load", () => {
  lottie.loadAnimation({
    container: document.getElementById("lottie-attention"), // 動畫容器
    renderer: "svg",
    loop: true,
    autoplay: true,
    animationData: animationData, // 使用 import 進來的動畫資料
  });
});
