import { ApiFailure } from '../api/client'
import type { PublicWorkCover } from '../api/client'

/** 将后端返回的 `/api/...` 封面或资源路径适配到当前站点的 BASE_URL。 */
export function resolvePublicAssetUrl(apiPath: string): string {
  if (apiPath.startsWith('/')) {
    return `${import.meta.env.BASE_URL}${apiPath.slice(1)}`
  }
  return apiPath
}

/** 去掉 `/api` 前缀供 downloadApiFile 复用（downloadApiFile 会自动拼 BASE_URL + api）。 */
export function stripApiPrefix(apiPath: string): string {
  if (apiPath.startsWith('/api/')) return apiPath.slice(4)
  return apiPath
}

/** 判断作者填写的公开链接是否为站外地址；无法解析时默认按站外处理。 */
export function isExternalWorkLink(rawUrl: string): boolean {
  try {
    const parsed = new URL(rawUrl, window.location.href)
    return parsed.origin !== window.location.origin
  } catch {
    return true
  }
}

/** 提取链接的主机名做轻量提示（不发请求、不测活）。 */
export function workLinkHost(rawUrl: string): string {
  try {
    return new URL(rawUrl).host
  } catch {
    return ''
  }
}

/** 计算封面 CSS aspect-ratio，防止图片加载中或失败占位造成页面跳动。 */
export function formatWorkCoverRatio(cover: Pick<PublicWorkCover, 'width' | 'height'>): string {
  const width = cover.width > 0 ? cover.width : 16
  const height = cover.height > 0 ? cover.height : 9
  return `${width} / ${height}`
}

/** 仅当作品列表接口返回 404 时才判定为旧后端尚未开放作品端点。 */
export function isLegacyWorksMissing(error: unknown): boolean {
  return error instanceof ApiFailure && error.status === 404
}

/** 作品详情接口 404 表示该作品已撤下、未公开或不存在。 */
export function isWorkNotFound(error: unknown): boolean {
  return error instanceof ApiFailure && error.status === 404
}

/** 文件下载接口 404 表示当前文件已不在公开版本中，不代表整件作品被撤下。 */
export function isWorkAssetNotFound(error: unknown): boolean {
  return error instanceof ApiFailure && error.status === 404
}
