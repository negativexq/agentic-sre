{{- define "connector.name" -}}{{ .Release.Name }}{{- end -}}
{{- define "connector.labels" -}}
app.kubernetes.io/name: agentic-sre-connector
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}
{{- define "connector.selector" -}}
app.kubernetes.io/name: agentic-sre-connector
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "connector.credentials" -}}{{ .Release.Name }}-credentials{{- end -}}
{{- define "connector.enrollmentSecret" -}}{{ .Values.enrollment.existingSecret | default (printf "%s-enrollment" .Release.Name) }}{{- end -}}
{{- define "connector.webhookSecret" -}}{{ .Values.webhook.existingSecret | default (printf "%s-webhook" .Release.Name) }}{{- end -}}
{{- define "connector.allNamespaces" -}}{{ concat .Values.watch.namespaces .Values.watch.evidenceNamespaces | uniq | join "," }}{{- end -}}
