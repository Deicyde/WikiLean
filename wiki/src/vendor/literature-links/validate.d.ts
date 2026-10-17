declare const validate: {
  (value: unknown): boolean;
  errors: Array<{ instancePath: string; message?: string }> | null;
};
export default validate;
