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
{{- $l := .Values.llm | default dict -}}
{{- $e := .Values.embeddings | default dict -}}
{{- $needsOllama := or (eq ($l.provider | default "ollama") "ollama") (eq ($e.provider | default "ollama") "ollama") -}}
{{- if .Values.ollama.enabled -}}
http://ollama.{{ .Release.Namespace }}.svc:11434
{{- else if $needsOllama -}}
{{- required "ollama.externalUrl is required when ollama.enabled=false (or use hosted providers for llm and embeddings)" .Values.ollama.externalUrl -}}
{{- else -}}
{{- .Values.ollama.externalUrl | default "" -}}
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

{{/* true when the gateway calls a hosted AI provider (chat or embeddings) */}}
{{- define "kubelantern-ai.hostedAI" -}}
{{- $l := .Values.llm | default dict -}}
{{- $e := .Values.embeddings | default dict -}}
{{- if or (ne ($l.provider | default "ollama") "ollama") (ne ($e.provider | default "ollama") "ollama") -}}true{{- end -}}
{{- end }}

{{/*
Storage for a component (Ollama models, Qdrant data).
Call with (dict "root" $ "p" .Values.x.persistence "name" "ollama-models" "component" "ollama").
persistence.type: pvc (default) | existingClaim | nfs | hostPath | emptyDir | custom
*/}}
{{- define "kubelantern-ai.storageType" -}}
{{- if not .p.enabled -}}emptyDir{{- else -}}{{- .p.type | default "pvc" -}}{{- end -}}
{{- end }}

{{/* PersistentVolume / PersistentVolumeClaim objects the chart creates (if any). */}}
{{- define "kubelantern-ai.storageObjects" -}}
{{- $t := include "kubelantern-ai.storageType" . -}}
{{- $p := .p -}}
{{- $nfs := $p.nfs | default dict -}}
{{- $pvName := printf "%s-%s-%s" .root.Release.Name .root.Release.Namespace .name -}}
{{- if and (eq $t "nfs") $nfs.createPersistentVolume }}
# Static NFS PersistentVolume (cluster-scoped; kept on uninstall: Retain).
apiVersion: v1
kind: PersistentVolume
metadata:
  name: {{ $pvName }}
  labels:
    app.kubernetes.io/name: {{ .component }}
    {{- include "kubelantern-ai.labels" .root | nindent 4 }}
spec:
  capacity:
    storage: {{ $p.size }}
  accessModes: {{ toYaml ($p.accessModes | default (list "ReadWriteMany")) | nindent 4 }}
  persistentVolumeReclaimPolicy: Retain
  storageClassName: ""
  {{- with $nfs.mountOptions }}
  mountOptions: {{ toYaml . | nindent 4 }}
  {{- end }}
  nfs:
    server: {{ required "persistence.nfs.server is required for type nfs" $nfs.server | quote }}
    path: {{ required "persistence.nfs.path is required for type nfs" $nfs.path | quote }}
---
{{- end }}
{{- if or (eq $t "pvc") (and (eq $t "nfs") $nfs.createPersistentVolume) }}
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {{ .name }}
  namespace: {{ .root.Release.Namespace }}
  labels:
    app.kubernetes.io/name: {{ .component }}
    {{- include "kubelantern-ai.labels" .root | nindent 4 }}
  {{- with $p.annotations }}
  annotations: {{ toYaml . | nindent 4 }}
  {{- end }}
spec:
  {{- if eq $t "nfs" }}
  accessModes: {{ toYaml ($p.accessModes | default (list "ReadWriteMany")) | nindent 4 }}
  storageClassName: ""
  volumeName: {{ $pvName }}
  {{- else }}
  accessModes: {{ toYaml ($p.accessModes | default (list "ReadWriteOnce")) | nindent 4 }}
  {{- with $p.storageClass }}
  storageClassName: {{ . | quote }}
  {{- end }}
  {{- end }}
  resources:
    requests:
      storage: {{ $p.size }}
---
{{- end }}
{{- end }}

{{/* The pod volume source (goes under `- name: <volume>`). */}}
{{- define "kubelantern-ai.volumeSource" -}}
{{- $t := include "kubelantern-ai.storageType" . -}}
{{- $p := .p -}}
{{- $nfs := $p.nfs | default dict -}}
{{- if or (eq $t "pvc") (and (eq $t "nfs") $nfs.createPersistentVolume) }}
persistentVolumeClaim:
  claimName: {{ .name }}
{{- else if eq $t "existingClaim" }}
persistentVolumeClaim:
  claimName: {{ required "persistence.existingClaim is required for type existingClaim" $p.existingClaim }}
{{- else if eq $t "nfs" }}
nfs:
  server: {{ required "persistence.nfs.server is required for type nfs" $nfs.server | quote }}
  path: {{ required "persistence.nfs.path is required for type nfs" $nfs.path | quote }}
{{- else if eq $t "hostPath" }}
{{- $h := $p.hostPath | default dict }}
hostPath:
  path: {{ required "persistence.hostPath.path is required for type hostPath" $h.path | quote }}
  type: {{ $h.type | default "DirectoryOrCreate" }}
{{- else if eq $t "custom" }}
{{- required "persistence.custom (a volume source) is required for type custom" $p.custom | toYaml }}
{{- else }}
{{- $e := $p.emptyDir | default dict }}
{{- if or $e.sizeLimit $e.medium }}
emptyDir:
  {{- with $e.sizeLimit }}
  sizeLimit: {{ . }}
  {{- end }}
  {{- with $e.medium }}
  medium: {{ . }}
  {{- end }}
{{- else }}
emptyDir: {}
{{- end }}
{{- end }}
{{- end }}
