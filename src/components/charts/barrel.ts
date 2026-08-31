import { lazy } from "react"

export const Area = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.Area }
})
export const AreaChart = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.AreaChart }
})
export const Bar = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.Bar }
})
export const BarChart = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.BarChart }
})
export const CartesianGrid = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.CartesianGrid }
})
export const Legend = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.Legend }
})
export const Line = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.Line }
})
export const LineChart = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.LineChart }
})
export const Pie = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.Pie }
})
export const PieChart = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.PieChart }
})
export const Tooltip = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.Tooltip }
})
export const XAxis = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.XAxis }
})
export const YAxis = lazy(async () => {
  const mod = await import("recharts")
  return { default: mod.YAxis }
})
export { ChartFrame } from "./chart-frame"
