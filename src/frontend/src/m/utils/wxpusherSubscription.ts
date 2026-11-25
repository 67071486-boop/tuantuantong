export function openWxPusherSubscription(url: string): void {
  try {
    const target = new URL(url)
    if (!target.username && !target.password
      && (target.origin === 'https://wxpusher.zjiecode.com' || target.origin === window.location.origin)) {
      window.location.assign(target.href)
    }
  } catch {
    // 页面保留链接入口，浏览器拦截时可以手动打开。
  }
}
