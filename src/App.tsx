import { RouterProvider } from "@tanstack/react-router"
import { Toaster } from "sonner"

import { router } from "@/router"

export function App() {
  return (
    <>
      <RouterProvider router={router} />
      <Toaster position="top-center" richColors closeButton />
    </>
  )
}

export default App
