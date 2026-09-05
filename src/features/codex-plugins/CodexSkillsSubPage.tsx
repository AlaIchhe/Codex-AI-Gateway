import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Link2, Loader2, Plus, Trash2, Wrench } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { Alert, AlertDescription } from "@/components/coss/components/alert"
import { Badge } from "@/components/coss/components/badge"
import { Button } from "@/components/coss/components/button"
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/coss/components/card"
import {
  Dialog,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogPanel,
  DialogPopup,
  DialogTitle,
} from "@/components/coss/components/dialog"
import { Input } from "@/components/coss/components/input"
import { Label } from "@/components/coss/components/label"
import { Textarea } from "@/components/coss/components/textarea"
import { api } from "@/lib/api"

function CreateSkillDialog({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient()
  const [skillId, setSkillId] = useState("")
  const [name, setName] = useState("")
  const [description, setDescription] = useState("")

  const create = useMutation({
    mutationFn: () =>
      api.createCodexSkill({
        skill_id: skillId.trim(),
        name: name.trim() || undefined,
        description: description.trim() || undefined,
      }),
    onSuccess: () => {
      toast.success("技能已创建。")
      void queryClient.invalidateQueries({ queryKey: ["codex-skills"] })
      onClose()
    },
    onError: (error: Error) => toast.error(error.message),
  })

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogPopup className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>新增技能</DialogTitle>
          <DialogDescription>
            在本机 skills 目录创建技能骨架（SKILL.md），Codex 重启后可发现。
          </DialogDescription>
        </DialogHeader>
        <DialogPanel className="space-y-3">
          <div className="space-y-1.5">
            <Label htmlFor="skill-id">技能 ID</Label>
            <Input
              id="skill-id"
              value={skillId}
              onChange={(event) => setSkillId(event.target.value)}
              placeholder="my-skill"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="skill-name">名称（可选）</Label>
            <Input
              id="skill-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="My Skill"
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="skill-description">描述（可选）</Label>
            <Textarea
              id="skill-description"
              rows={3}
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="什么时候使用这个技能…"
            />
          </div>
        </DialogPanel>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            取消
          </Button>
          <Button
            disabled={create.isPending || !skillId.trim()}
            onClick={() => create.mutate()}
          >
            {create.isPending ? (
              <Loader2 className="size-4 animate-spin" />
            ) : null}
            创建
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  )
}

export function CodexSkillsSubPage() {
  const queryClient = useQueryClient()
  const [creating, setCreating] = useState(false)

  const query = useQuery({
    queryKey: ["codex-skills"],
    queryFn: api.listCodexSkills,
  })

  const remove = useMutation({
    mutationFn: api.deleteCodexSkill,
    onSuccess: () => {
      toast.success("技能已删除。")
      void queryClient.invalidateQueries({ queryKey: ["codex-skills"] })
    },
    onError: (error: Error) => toast.error(error.message),
  })

  const data = query.data

  return (
    <>
      {query.isLoading ? (
        <div className="text-sm text-muted-foreground">加载中…</div>
      ) : query.isError ? (
        <Alert variant="error">
          <AlertDescription>Codex 技能加载失败。</AlertDescription>
        </Alert>
      ) : !data?.exists ? (
        <Alert variant="warning">
          <AlertDescription>
            未找到技能目录：{data?.skills_dir ?? "skills"}
            。可直接新增技能，目录会自动创建。
          </AlertDescription>
        </Alert>
      ) : data.skills.length ? (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              技能
              <Badge variant="secondary">{data.skills.length}</Badge>
            </CardTitle>
            <CardDescription
              className="max-w-xl truncate"
              title={data.skills_dir}
            >
              {data.skills_dir}
            </CardDescription>
            <CardAction>
              <Button size="sm" onClick={() => setCreating(true)}>
                <Plus className="size-4" />
                新增
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {data.skills.map((skill) => (
                <div
                  key={skill.id}
                  className="flex flex-col gap-2 rounded-xl border bg-card p-4"
                >
                  <div className="flex items-start gap-2.5">
                    <div className="flex size-8 shrink-0 items-center justify-center rounded-lg border bg-muted/50">
                      <Wrench className="size-4 text-muted-foreground" />
                    </div>
                    <div className="min-w-0">
                      <div className="truncate font-medium">{skill.name}</div>
                      <div
                        className="truncate text-xs text-muted-foreground"
                        title={skill.id}
                      >
                        {skill.id}
                      </div>
                    </div>
                    <div className="ml-auto flex shrink-0 items-center gap-1">
                      {skill.is_symlink ? (
                        <Link2
                          className="size-3.5 text-muted-foreground"
                          aria-label="符号链接（由插件或外部工具提供）"
                        />
                      ) : null}
                      <Button
                        size="icon-sm"
                        variant="ghost"
                        disabled={remove.isPending}
                        aria-label={`删除 ${skill.id}`}
                        onClick={() => remove.mutate(skill.id)}
                      >
                        <Trash2 className="size-4" />
                      </Button>
                    </div>
                  </div>
                  {skill.description ? (
                    <p className="line-clamp-2 text-sm text-muted-foreground">
                      {skill.description}
                    </p>
                  ) : null}
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>技能</CardTitle>
            <CardDescription
              className="max-w-xl truncate"
              title={data.skills_dir}
            >
              {data.skills_dir}
            </CardDescription>
            <CardAction>
              <Button size="sm" onClick={() => setCreating(true)}>
                <Plus className="size-4" />
                新增
              </Button>
            </CardAction>
          </CardHeader>
          <CardContent>
            <div className="text-sm text-muted-foreground">技能目录为空。</div>
          </CardContent>
        </Card>
      )}

      {creating ? (
        <CreateSkillDialog onClose={() => setCreating(false)} />
      ) : null}
    </>
  )
}
