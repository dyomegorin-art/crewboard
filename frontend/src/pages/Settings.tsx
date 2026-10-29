import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import { getAccessToken } from "../api/client";
import { Button, Card, ErrorBox, Field, Select } from "../components/ui";
import { useWorkspace } from "../hooks/workspace";
import { errorMessage } from "../lib/format";
import { promptUpgrade, toast } from "../lib/ui-events";

export function SettingsPage() {
  const { ws, can, readOnly } = useWorkspace();
  const qc = useQueryClient();
  const nav = useNavigate();
  const [name, setName] = useState(ws.name);
  const [newOwner, setNewOwner] = useState("");
  const members = useQuery({ queryKey: ["members", ws.id], queryFn: () => api.members(ws.id), enabled: can("workspace.transfer") });
  const done = () => {
    void qc.invalidateQueries({ queryKey: ["workspaces"] });
    void qc.invalidateQueries({ queryKey: ["workspace", ws.id] });
  };
  const rename = useMutation({ mutationFn: () => api.renameWorkspace(ws.id, name), onSuccess: () => { done(); toast("Сохранено"); } });
  const transfer = useMutation({ mutationFn: () => api.transferOwnership(ws.id, Number(newOwner)), onSuccess: () => { done(); void qc.invalidateQueries({ queryKey: ["members", ws.id] }); toast("Владение передано"); } });
  const remove = useMutation({ mutationFn: () => api.deleteWorkspace(ws.id), onSuccess: async () => { await qc.invalidateQueries({ queryKey: ["workspaces"] }); nav("/app"); } });

  const exportCsv = async () => {
    if (ws.plan !== "pro") return promptUpgrade({ code: "plan_limit_export", message: "Экспорт CSV доступен на тарифе Pro" });
    const res = await fetch(api.exportCsvUrl(ws.id), { headers: { Authorization: `Bearer ${getAccessToken() ?? ""}` } });
    if (!res.ok) return toast("Не удалось выгрузить CSV", "error");
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = "tasks.csv";
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="mx-auto max-w-2xl space-y-5 p-4 sm:p-6">
      <h1 className="text-xl font-semibold">Настройки</h1>
      <Card title="Воркспейс">
        <form onSubmit={(e) => { e.preventDefault(); rename.mutate(); }} className="space-y-3">
          {rename.isError && <ErrorBox message={errorMessage(rename.error)} />}
          <Field label="Название" value={name} onChange={(e) => setName(e.target.value)} disabled={readOnly} />
          <Button type="submit" loading={rename.isPending} disabled={readOnly || !name.trim() || name === ws.name}>Сохранить</Button>
        </form>
      </Card>
      <Card title="Данные">
        <p className="mb-3 text-sm text-muted">Выгрузка всех задач воркспейса в CSV{ws.plan === "pro" ? "." : " — на тарифе Pro."}</p>
        <Button variant="secondary" onClick={() => void exportCsv()}>Экспорт задач в CSV</Button>
      </Card>
      {can("workspace.transfer") && (
        <Card title="Владение и удаление">
          <div className="space-y-4">
            <div className="flex flex-wrap items-end gap-2">
              <Select label="Передать владение" value={newOwner} onChange={(e) => setNewOwner(e.target.value)}>
                <option value="">Выберите участника</option>
                {members.data?.filter((m) => !m.is_owner && m.role !== "guest").map((m) => <option key={m.user_id} value={m.user_id}>{m.name}</option>)}
              </Select>
              <Button variant="secondary" disabled={!newOwner || readOnly} loading={transfer.isPending} onClick={() => window.confirm("Передать владение? Вы станете админом.") && transfer.mutate()}>Передать</Button>
            </div>
            {transfer.isError && <ErrorBox message={errorMessage(transfer.error)} />}
            {remove.isError && <ErrorBox message={errorMessage(remove.error)} />}
            <Button variant="danger" onClick={() => window.confirm(`Удалить воркспейс «${ws.name}» вместе со всеми данными?`) && remove.mutate()}>Удалить воркспейс</Button>
          </div>
        </Card>
      )}
    </div>
  );
}
