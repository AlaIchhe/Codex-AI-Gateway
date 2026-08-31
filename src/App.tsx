import { RouterProvider } from "@tanstack/react-router"
import { MotionConfig } from "motion/react"
import { Toaster } from "sonner"

import { router } from "@/router"

export function App() {
  return (
    <MotionConfig reducedMotion="user">
      <RouterProvider router={router} />
      <Toaster position="top-center" richColors closeButton />
    </MotionConfig>
  )
}

export default App
