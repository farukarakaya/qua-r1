// SPDX-License-Identifier: Apache-2.0

import { expect, test } from "bun:test"
import { testRender } from "@opentui/solid"
import { newTurn } from "../lib/conversation"
import { Transcript } from "./transcript"

test("only a transcript drag copies the highlighted Markdown text on release", async () => {
  const copied: string[] = []
  const turn = newTurn("Question", "answered")
  turn.parts = [{ kind: "text", text: "Alpha **bravo**\n\nCharlie delta" }]
  const { renderer, renderOnce, captureCharFrame, mockMouse } = await testRender(
    () => (
      <box flexDirection="column">
        <text>Outside transcript</text>
        <Transcript turns={[turn]} onCopy={(text) => copied.push(text)} showReasoning toolDetails="brief" />
      </box>
    ),
    { width: 60, height: 20 },
  )
  try {
    // Markdown parsing completes asynchronously after the first frame.
    for (let i = 0; i < 30 && !captureCharFrame().includes("Charlie delta"); i++) {
      await renderOnce()
      await Bun.sleep(5)
    }
    expect(captureCharFrame()).toContain("Charlie delta")
    const lines = captureCharFrame().split("\n")
    const position = (text: string) => {
      const y = lines.findIndex((line) => line.includes(text))
      return [lines[y].indexOf(text), y] as const
    }
    const [x, y] = position("bravo")
    await mockMouse.moveTo(x, y)
    await mockMouse.click(x, y)
    const [outsideX, outsideY] = position("Outside transcript")
    await mockMouse.drag(outsideX, outsideY, outsideX + 7, outsideY)
    expect(copied).toEqual([])
    const [endX, endY] = position("e delta")
    await mockMouse.drag(x, y, endX, endY)
    expect(copied).toEqual(["bravo\nCharlie"])
  } finally {
    renderer.destroy()
  }
})
