import { useLocation, useNavigate } from "@tanstack/react-router"

export function useOverlaySearch(key: string) {
  const location = useLocation()
  const navigate = useNavigate()

  const params = new URLSearchParams(location.search)
  const value = params.get(key) ?? undefined

  function setValue(next: string | boolean | null | undefined) {
    const nextParams = new URLSearchParams(location.search)
    if (next === false || next === null || next === undefined || next === "")
      nextParams.delete(key)
    else nextParams.set(key, next === true ? "1" : next)
    const query = nextParams.toString()
    void navigate({
      href: `${location.pathname}${query ? `?${query}` : ""}`,
      replace: true,
    })
  }

  return [value, setValue] as const
}
