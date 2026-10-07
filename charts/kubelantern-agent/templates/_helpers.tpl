{{/*
Resource names are fixed ("kubelantern-agent"): one agent per namespace, and the
gateway's NetworkPolicy and TokenReview check match this exact name.
*/}}
{{- define "kubelantern-agent.name" -}}kubelantern-agent{{- end }}

{{- define "kubelantern-agent.labels" -}}
app.kubernetes.io/name: kubelantern-agent
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: kubelantern
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end }}

{{- define "kubelantern-agent.selectorLabels" -}}
app.kubernetes.io/name: kubelantern-agent
{{- end }}

{{- define "kubelantern-agent.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}
{{- end }}
