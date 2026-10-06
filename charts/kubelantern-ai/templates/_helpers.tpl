{{/*
Service names are fixed (kubelantern-gateway, ollama, qdrant): the agent chart's
default gateway URL and the NetworkPolicies refer to them.
*/}}
{{- define "kubelantern-ai.labels" -}}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: kubelantern
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "kubelantern-ai.ollamaUrl" -}}
{{- if .Values.ollama.enabled -}}
http://ollama.{{ .Release.Namespace }}.svc:11434
{{- else -}}
{{- required "ollama.externalUrl is required when ollama.enabled=false" .Values.ollama.externalUrl -}}
{{- end -}}
{{- end }}

{{- define "kubelantern-ai.qdrantUrl" -}}
{{- if .Values.qdrant.enabled -}}
http://qdrant.{{ .Release.Namespace }}.svc:6333
{{- end -}}
{{- end }}

{{/* Common restricted pod securityContext for KubeLantern's own images. */}}
{{- define "kubelantern-ai.restrictedContainer" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}
