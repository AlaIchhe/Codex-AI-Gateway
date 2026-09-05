import { useQuery } from "@tanstack/react-query"
import { Link2, Wrench } from "lucide-react"

import { Alert, AlertDescription } from "@/components/coss/components/alert"
import { Badge } from "@/components/coss/components/badge"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/coss/components/card"
import { api } from "@/lib/api"

export function CodexSkillsSubPage() {
  const query = useQuery({
    queryKey: ["codex-skills"],
    queryFn: api.listCodexSkills,
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
            。安装技能或插件后会自动创建。
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
                    {skill.is_symlink ? (
                      <Link2
                        className="ml-auto size-3.5 shrink-0 text-muted-foreground"
                        aria-label="符号链接"
                      />
                    ) : null}
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
        <div className="text-sm text-muted-foreground">
          技能目录为空：{data.skills_dir}
        </div>
      )}
    </>
  )
}
