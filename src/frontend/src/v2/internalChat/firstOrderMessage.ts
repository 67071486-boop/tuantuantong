import type { OrderChatImage, OrderChatSendBody } from '@/api/orderChat'

export type FirstOrderMessageDraft = { text: string; image?: OrderChatImage; imageName?: string; attempt?: OrderChatSendBody }

const IMAGE_MAX_BYTES = 3 * 1024 * 1024
const SOURCE_MAX_BYTES = 10 * 1024 * 1024
const MAX_IMAGE_PIXELS = 16_000_000
const IMAGE_READ_ERROR = '无法读取这张图片，请重新截图或另存后发送'

function uploadMime(bytes: Uint8Array): OrderChatImage['mime'] | undefined {
  const matches = (signature: number[], offset = 0) => signature.every((byte, index) => bytes[offset + index] === byte)
  if (matches([0xff, 0xd8, 0xff])) return 'image/jpeg'
  if (matches([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return 'image/png'
  if (matches([0x52, 0x49, 0x46, 0x46]) && matches([0x57, 0x45, 0x42, 0x50], 8)) return 'image/webp'
}

async function decodeImage(blob: Blob): Promise<HTMLImageElement> {
  const source = URL.createObjectURL(blob)
  try {
    const image = new Image()
    image.src = source
    await image.decode()
    return image
  } finally {
    URL.revokeObjectURL(source)
  }
}

async function compressImage(image: HTMLImageElement): Promise<{ blob: Blob; mime: OrderChatImage['mime'] }> {
  const canvas = document.createElement('canvas')
  const context = canvas.getContext('2d')
  const { naturalWidth: width, naturalHeight: height } = image
  let scale = Math.min(1, 4096 / Math.max(width, height))
  try {
    if (context) {
      for (let attempt = 0; attempt < 6; attempt += 1, scale *= 0.7) {
        canvas.width = Math.max(1, Math.round(width * scale))
        canvas.height = Math.max(1, Math.round(height * scale))
        context.drawImage(image, 0, 0, canvas.width, canvas.height)
        const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/webp', 0.85))
        if (blob?.size && blob.size <= IMAGE_MAX_BYTES && (blob.type === 'image/webp' || blob.type === 'image/png')) {
          return { blob, mime: blob.type }
        }
      }
    }
  } finally {
    canvas.width = canvas.height = 0
  }
  throw new Error('无法将图片压缩到 3 MiB，请缩小图片后重试')
}

export async function encodeFirstOrderImage(file: File): Promise<OrderChatImage> {
  if (!file.size) throw new Error('图片为空')
  if (file.size > SOURCE_MAX_BYTES) throw new Error('原图片不能超过 10 MiB')
  const mime = uploadMime(new Uint8Array(await file.slice(0, 12).arrayBuffer()))
  const image = await decodeImage(file.slice(0, file.size, mime)).catch(() => {
    // SVG 需要声明类型才会在图片元素中解码。
    if (!mime) return decodeImage(file.slice(0, file.size, 'image/svg+xml'))
    throw new Error(IMAGE_READ_ERROR)
  }).catch(() => { throw new Error(IMAGE_READ_ERROR) })
  if (image.naturalWidth * image.naturalHeight > MAX_IMAGE_PIXELS) throw new Error('图片分辨率过大')

  const prepared = mime && file.size <= IMAGE_MAX_BYTES ? { blob: file, mime } : await compressImage(image)
  const data = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('读取图片失败'))
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] ?? '')
    reader.readAsDataURL(prepared.blob)
  })
  return { mime: prepared.mime, data }
}
