// SPDX-License-Identifier: Apache-2.0

import { expect, spyOn, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { App } from "./app"

test("PDF access refreshes a saved session and can be enabled before the first turn", async () => {
  let enabled = true
  let freshEnabled = false
  const flags = () => ({ pdfs_enabled: enabled, pdfs_private: true })
  const session = { id: "demo", title: "Saved chat", turns: 1, updated_at: "2026-01-01" }
  const backend = async (input: Parameters<typeof fetch>[0], init?: RequestInit) => {
    const url = new URL(String(input))
    switch (url.pathname) {
      case "/models":
        return Response.json({ models: [{ name: "test", model: "test", ready: true }], default: "test" })
      case "/sessions":
        return Response.json(init?.method === "POST" ? { id: "fresh" } : { sessions: [session] })
      case "/sessions/demo":
        return Response.json({ turns: [], pdfs_enabled: false, pdfs_private: true })
      case "/sessions/demo/pdfs":
        return Response.json(flags())
      case "/sessions/fresh/pdfs":
        if (init?.method === "PUT") freshEnabled = JSON.parse(String(init.body)).enabled === true
        return Response.json({ pdfs_enabled: freshEnabled, pdfs_private: freshEnabled })
      case "/status":
        const id = url.searchParams.get("session_id")
        const freshPdfs = id === "fresh" && freshEnabled
        return Response.json({
          ...(id === "demo" ? flags() : { pdfs_enabled: freshPdfs, pdfs_private: freshPdfs }),
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

      mockInput.pressEnter() // Cancel is selected by default.
      await renderOnce()
      await command("New session")
      await command("PDF access")
      mockInput.pressArrow("down")
      mockInput.pressEnter()
      await renderOnce()
      expect(freshEnabled).toBe(true)
      expect(captureCharFrame()).toContain("PDFs on, sync off")
    } finally {
      renderer.destroy()
    }
  } finally {
    fetchMock.mockRestore()
  }
})
