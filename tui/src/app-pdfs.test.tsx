// SPDX-License-Identifier: Apache-2.0

import { expect, spyOn, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { App } from "./app"

test("status refresh and the PDF dialog reflect another client's changes", async () => {
  let enabled = true
  const flags = () => ({ pdfs_enabled: enabled, pdfs_private: true })
  const session = { id: "demo", title: "Saved chat", turns: 1, updated_at: "2026-01-01" }
  const backend = async (input: Parameters<typeof fetch>[0]) => {
    const url = new URL(String(input))
    switch (url.pathname) {
      case "/models":
        return Response.json({ models: [{ name: "test", model: "test", ready: true }], default: "test" })
      case "/sessions":
        return Response.json({ sessions: [session] })
      case "/sessions/demo":
        return Response.json({ turns: [], pdfs_enabled: false, pdfs_private: true })
      case "/sessions/demo/pdfs":
        return Response.json(flags())
      case "/status":
        return Response.json({
          ...(url.searchParams.has("session_id") ? flags() : { pdfs_enabled: false, pdfs_private: false }),
          model: "test",
          tokens_used: 0,
          context_window: 64000,
          used_pct: 0,
          compact_at: null,
          turns: 0,
        })
      default:
        return Response.json({})
    }
  }
  const fetchMock = spyOn(globalThis, "fetch").mockImplementation(
    Object.assign(backend, { preconnect: fetch.preconnect }),
  )
  try {
    const { renderer, renderOnce, captureCharFrame, mockInput } = await testRender(() => <App />, {
      width: 110,
      height: 40,
    })
    try {
      const command = async (name: string) => {
        mockInput.pressKey("k", { ctrl: true })
        await renderOnce()
        await mockInput.typeText(name)
        mockInput.pressEnter()
        await renderOnce()
      }
      await renderOnce()
      await command("resume")
      mockInput.pressEnter()
      await renderOnce()
      expect(captureCharFrame()).toContain("PDFs on, sync off") // /status, after replay reported off
      enabled = false // a different client disables access between turns
      await command("PDF access")
      expect(captureCharFrame()).toContain("Enable PDFs")
      expect(captureCharFrame()).toContain("PDFs off, sync off")
    } finally {
      renderer.destroy()
    }
  } finally {
    fetchMock.mockRestore()
  }
})
